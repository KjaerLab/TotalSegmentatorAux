import os
import sys
import time
import shutil
import zipfile
from pathlib import Path
import subprocess
import platform
import importlib.metadata

from tqdm import tqdm
import numpy as np
import nibabel as nib
import dicom2nifti

from totalsegmentator.config import get_weights_dir
from totalsegmentator.dicom_utils import rgb_to_cielab_dicom, generate_random_color, load_snomed_mapping


def command_exists(command):
    return shutil.which(command) is not None


def download_dcm2niix():
    import urllib.request
    print("  Downloading dcm2niix...")

    if platform.system() == "Windows":
        # url = "https://github.com/rordenlab/dcm2niix/releases/latest/download/dcm2niix_win.zip"
        url = "https://github.com/rordenlab/dcm2niix/releases/download/v1.0.20230411/dcm2niix_win.zip"
    elif platform.system() == "Darwin":  # Mac
        # raise ValueError("For MacOS automatic installation of dcm2niix not possible. Install it manually.")
        if platform.machine().startswith("arm") or platform.machine().startswith("aarch"):  # arm
            # url = "https://github.com/rordenlab/dcm2niix/releases/latest/download/macos_dcm2niix.pkg"
            url = "https://github.com/rordenlab/dcm2niix/releases/download/v1.0.20230411/dcm2niix_macos.zip"
        else:  # intel
            # unclear if this is the right link (is the same as for arm)
            # url = "https://github.com/rordenlab/dcm2niix/releases/latest/download/macos_dcm2niix.pkg"
            url = "https://github.com/rordenlab/dcm2niix/releases/download/v1.0.20230411/dcm2niix_macos.zip"
    elif platform.system() == "Linux":
        # url = "https://github.com/rordenlab/dcm2niix/releases/latest/download/dcm2niix_lnx.zip"
        url = "https://github.com/rordenlab/dcm2niix/releases/download/v1.0.20230411/dcm2niix_lnx.zip"
    else:
        raise ValueError("Unknown operating system. Can not download the right version of dcm2niix.")

    config_dir = get_weights_dir()

    urllib.request.urlretrieve(url, config_dir / "dcm2niix.zip")
    with zipfile.ZipFile(config_dir / "dcm2niix.zip", 'r') as zip_ref:
        zip_ref.extractall(config_dir)

    # Give execution permission to the script
    if platform.system() == "Windows":
        os.chmod(config_dir / "dcm2niix.exe", 0o755)
    else:
        os.chmod(config_dir / "dcm2niix", 0o755)

    # Clean up
    if (config_dir / "dcm2niix.zip").exists():
        os.remove(config_dir / "dcm2niix.zip")
    if (config_dir / "dcm2niibatch").exists():
        os.remove(config_dir / "dcm2niibatch")


def dcm_to_nifti_LEGACY(input_path, output_path, verbose=False):
    """
    Uses dcm2niix (does not properly work on windows)

    input_path: a directory of dicom slices
    output_path: a nifti file path
    """
    verbose_str = "" if verbose else "> /dev/null"

    config_dir = get_weights_dir()

    if command_exists("dcm2niix"):
        dcm2niix = "dcm2niix"
    else:
        if platform.system() == "Windows":
            dcm2niix = config_dir / "dcm2niix.exe"
        else:
            dcm2niix = config_dir / "dcm2niix"
        if not dcm2niix.exists():
            download_dcm2niix()

    subprocess.call(f"\"{dcm2niix}\" -o {output_path.parent} -z y -f {output_path.name[:-7]} {input_path} {verbose_str}", shell=True)

    if not output_path.exists():
        print(f"Content of dcm2niix output folder ({output_path.parent}):")
        print(list(output_path.parent.glob("*")))
        raise ValueError("dcm2niix failed to convert dicom to nifti.")

    nii_files = list(output_path.parent.glob("*.nii.gz"))

    if len(nii_files) > 1:
        print("WARNING: Dicom to nifti resulted in several nifti files. Skipping files which contain ROI in filename.")
        for nii_file in nii_files:
            # output file name is "converted_dcm.nii.gz" so if ROI in name, then this can be deleted
            if "ROI" in nii_file.name:
                os.remove(nii_file)
                print(f"Skipped: {nii_file.name}")

    nii_files = list(output_path.parent.glob("*.nii.gz"))

    if len(nii_files) > 1:
        print("WARNING: Dicom to nifti resulted in several nifti files. Only using first one.")
        print([f.name for f in nii_files])
        for nii_file in nii_files[1:]:
            os.remove(nii_file)
        # todo: have to rename first file to not contain any counter which is automatically added by dcm2niix

    os.remove(str(output_path)[:-7] + ".json")


def dcm_to_nifti(input_path, output_path, tmp_dir=None, verbose=False):
    """
    Uses dicom2nifti package (also works on windows)

    input_path: a directory of dicom slices or a zip file of dicom slices or a bytes object of zip file
    output_path: a nifti file path
    tmp_dir: extract zip file to this directory, else to the same directory as the zip file. Needs to be set if input is a zip file.
    """
    # Check if input_path is a zip file and extract it
    if zipfile.is_zipfile(input_path):
        if tmp_dir is None:
            raise ValueError("tmp_dir must be set when input_path is a zip file or bytes object of zip file")
        if verbose: print(f"Extracting zip file: {input_path}")
        extract_dir = os.path.splitext(input_path)[0] if tmp_dir is None else tmp_dir / "extracted_dcm"
        with zipfile.ZipFile(input_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
            input_path = extract_dir
    
    # Convert to nifti
    dicom2nifti.dicom_series_to_nifti(input_path, output_path, reorient_nifti=True)


def save_mask_as_rtstruct(img_data, selected_classes, dcm_reference_file, output_path):
    """
    dcm_reference_file: a directory with dcm slices ??
    """
    from rt_utils import RTStructBuilder
    import logging
    logging.basicConfig(level=logging.WARNING)  # avoid messages from rt_utils

    # create new RT Struct - requires original DICOM
    rtstruct = RTStructBuilder.create_new(dicom_series_path=dcm_reference_file)

    # add mask to RT Struct
    for class_idx, class_name in tqdm(selected_classes.items()):
        binary_img = img_data == class_idx
        if binary_img.sum() > 0:  # only save none-empty images

            # rotate nii to match DICOM orientation
            binary_img = np.rot90(binary_img, 1, (0, 1))  # rotate segmentation in-plane

            # add segmentation to RT Struct
            rtstruct.add_roi(
                mask=binary_img,  # has to be a binary numpy array
                name=class_name
            )

    rtstruct.save(str(output_path))


def save_mask_as_dicomseg(img_data, selected_classes, dcm_reference_file, output_path, nifti_affine):
    """
    Save segmentation as DICOM SEG using highdicom library.
    
    Args:
        img_data: segmentation data (multilabel image)
        selected_classes: dict mapping class indices to class names
        dcm_reference_file: a directory with dcm slices
        output_path: output path for the DICOM SEG file
        nifti_affine: affine transformation matrix from nifti image
    """
    import highdicom as hd
    import pydicom
    from pydicom.sr.codedict import codes
    from highdicom.seg.content import SegmentDescription
    
    # Get TotalSegmentator version
    version = importlib.metadata.version("TotalSegmentator")
    
    # Load SNOMED CT codes mapping
    snomed_map = load_snomed_mapping()
    
    # Read reference DICOM series
    dcm_files = sorted(Path(dcm_reference_file).glob("*.dcm"))
    if len(dcm_files) == 0:
        # Try without extension
        dcm_files = sorted([f for f in Path(dcm_reference_file).iterdir() if f.is_file()])
    
    if len(dcm_files) == 0:
        raise ValueError(f"No DICOM files found in {dcm_reference_file}")
    
    # Load all DICOM slices
    source_images = [pydicom.dcmread(str(f)) for f in dcm_files]
    
    # Sort by Instance Number or Image Position Patient
    if hasattr(source_images[0], 'InstanceNumber'):
        source_images = sorted(source_images, key=lambda x: x.InstanceNumber)
    elif hasattr(source_images[0], 'ImagePositionPatient'):
        source_images = sorted(source_images, key=lambda x: float(x.ImagePositionPatient[2]))
    
    # Get the dimensions of the source DICOM images
    dcm_rows = source_images[0].Rows
    dcm_cols = source_images[0].Columns
    dcm_slices = len(source_images)
    
    # The segmentation comes in NIfTI orientation, need to reorient to match DICOM
    # NIfTI typically has shape that might need transposing to match DICOM rows x cols x slices
    seg_shape = img_data.shape
    
    # Transpose if needed to match DICOM orientation (rows, cols, slices)
    # Segmentation is typically (height, width, depth), DICOM expects (rows, cols, slices)
    if seg_shape == (dcm_cols, dcm_rows, dcm_slices):
        # Need to transpose to swap rows and cols
        img_data = np.transpose(img_data, (1, 0, 2))
    elif seg_shape != (dcm_rows, dcm_cols, dcm_slices):
        raise ValueError(f"Segmentation shape {seg_shape} does not match DICOM dimensions ({dcm_rows}, {dcm_cols}, {dcm_slices}). "
                        "Cannot create DICOM SEG with mismatched dimensions.")
    
    # Prepare segment descriptions
    segment_descriptions = []
    pixel_arrays = []
    
    for class_idx, class_name in tqdm(selected_classes.items(), desc="Preparing segments"):
        binary_mask = (img_data == class_idx).astype(np.uint8)
        
        if binary_mask.sum() > 0:  # only save non-empty segments
            
            # Generate random color for this segment
            random_rgb = generate_random_color()
            random_cielab = rgb_to_cielab_dicom(random_rgb)
            
            # Get SNOMED codes for this structure
            if class_name in snomed_map:
                snomed = snomed_map[class_name]
                
                # Create property category code
                property_category = hd.sr.CodedConcept(
                    value=snomed['property_category']['value'],
                    scheme_designator=snomed['property_category']['scheme'],
                    meaning=snomed['property_category']['meaning']
                )
                
                # Create property type code
                property_type = hd.sr.CodedConcept(
                    value=snomed['property_type']['value'],
                    scheme_designator=snomed['property_type']['scheme'],
                    meaning=snomed['property_type']['meaning']
                )
                
                # Create segment description
                # Note: The segment label already contains full descriptive name including laterality
                segment_desc = SegmentDescription(
                    segment_number=len(segment_descriptions) + 1,
                    segment_label=class_name,
                    segmented_property_category=property_category,
                    segmented_property_type=property_type,
                    algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
                    algorithm_identification=hd.AlgorithmIdentificationSequence(
                        name="TotalSegmentator",
                        version=version,
                        family=codes.DCM.ArtificialIntelligence
                    )
                )
                # Set the recommended display color
                segment_desc.RecommendedDisplayCIELabValue = list(random_cielab)
            else:
                # Fallback to generic codes if structure not in mapping
                segment_desc = SegmentDescription(
                    segment_number=len(segment_descriptions) + 1,
                    segment_label=class_name,
                    segmented_property_category=codes.SCT.Tissue,
                    segmented_property_type=codes.SCT.Tissue,
                    algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
                    algorithm_identification=hd.AlgorithmIdentificationSequence(
                        name="TotalSegmentator",
                        version=version,
                        family=codes.DCM.ArtificialIntelligence
                    )
                )
                # Set the recommended display color
                segment_desc.RecommendedDisplayCIELabValue = list(random_cielab)
            
            segment_descriptions.append(segment_desc)
            pixel_arrays.append(binary_mask)
    
    if len(segment_descriptions) == 0:
        raise ValueError("No non-empty segments found to save")
    
    # Stack all binary masks into a single array
    # highdicom expects: (slices, rows, cols, num_segments)
    # Stack segments along the first axis: (num_segments, rows, cols, slices)
    pixel_array = np.stack(pixel_arrays, axis=0)
    
    # Flip along x and z axes to correct coordinate system difference between NIfTI and DICOM
    # Flip axis 1 (rows/x-axis) and axis 3 (slices/z-axis)
    pixel_array = pixel_array[:, ::-1, :, ::-1]
    
    # Transpose to move segments to last dimension and rearrange axes: (slices, rows, cols, num_segments)
    pixel_array = np.transpose(pixel_array, (3, 1, 2, 0))
    
    # For debugging
    # print(f"  Source_images: {len(source_images)} slices, {source_images[0].Rows} rows, {source_images[0].Columns} cols")
    # print(f"  Pixel_array: {pixel_array.shape[0]} slices, {pixel_array.shape[1]} rows, {pixel_array.shape[2]} cols, {pixel_array.shape[3]} segments")
    
    # Create DICOM SEG
    # Note: highdicom will handle the proper encoding of the multi-frame structure
    seg = hd.seg.Segmentation(
        source_images=source_images,
        pixel_array=pixel_array,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=segment_descriptions,
        series_instance_uid=hd.UID(),
        series_number=100,
        sop_instance_uid=hd.UID(),
        instance_number=1,
        manufacturer="TotalSegmentator",
        manufacturer_model_name="TotalSegmentator",
        software_versions=version,
        device_serial_number="1"
    )
    
    # Save DICOM SEG file
    seg.save_as(str(output_path))
