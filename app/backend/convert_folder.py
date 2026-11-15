import time
import psutil  # To monitor system resources
from PyPDF2 import PdfReader, PdfWriter
from pdf2image import convert_from_path
from pytesseract import image_to_pdf_or_hocr, pytesseract, Output
from io import BytesIO
from PIL import Image
import logging
import subprocess
import os
import shutil
import traceback

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('convert_folder.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

# Path to Tesseract
pytesseract.tesseract_cmd = '/usr/bin/tesseract'
poppler_path = '/usr/bin/'  # Adjust for your Raspberry Pi environment

# Define thresholds for resource usage (adjust as needed for your Pi)
CPU_THRESHOLD = 75  # Max CPU usage percentage
MEMORY_THRESHOLD = 95  # Max memory usage percentage

def wait_for_resources():
    """
    Wait until system resources are below thresholds.
    """
    retry_counter=0
    while retry_counter<20:
        cpu_usage = psutil.cpu_percent(interval=1)
        memory_usage = psutil.virtual_memory().percent

        if cpu_usage < CPU_THRESHOLD and memory_usage < MEMORY_THRESHOLD:
            return
        logger.info(f"High resource usage detected: CPU {cpu_usage}%, Memory {memory_usage}%. Waiting...")
        time.sleep(5)  # Wait before rechecking
        retry_counter+=1
    logger.error("Aborted because of ressource constraints")
    raise Exception("System has performance issues")

def correct_image_orientation(image):
    """
    Corrects the orientation of an image using Tesseract's orientation detection.

    Args:
        image (PIL.Image.Image): The image to analyze and correct.

    Returns:
        PIL.Image.Image: The corrected image.
    """
    try:
        # Detect orientation
        data = pytesseract.image_to_osd(image, output_type=Output.DICT)
        rotation = int(data.get("rotate", 0))

        if rotation != 0:
            # Rotate image to correct orientation
            logger.info(f"Correcting image orientation by {rotation} degrees.")
            return image.rotate(-rotation, expand=True)
        return image
    except Exception as e:
        logger.error(f"Error detecting orientation: {e}", exc_info=True)
        return image  # Return the original image if detection fails


def get_pdf_page_count(pdf_path):
    """
    Get the number of pages using pdfinfo (poppler-utils).
    Ultra-low memory usage.
    """
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        for line in result.stdout.split('\n'):
            if line.startswith('Pages:'):
                return int(line.split(':')[1].strip())
    except Exception as e:
        logger.error(f"Failed to count pages with pdfinfo: {e}")
        raise


def convert_pdf_to_searchable(input_pdf_path, output_pdf_path, should_pause=None):
    """
    Apply OCR to a PDF step-by-step to ensure stability and low resource usage,
    with added orientation correction.
    """
    logger.info(f"Starting OCR for: {input_pdf_path}")

    try:

        # Read the input PDF to get the total number of pages
        pdfinfopages=get_pdf_page_count(input_pdf_path)
        logger.info(f"According to pdfinfo this has '{pdfinfopages}' pages")
        reader = PdfReader(input_pdf_path)

        # Handle encrypted PDFs
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # Attempt to decrypt with an empty password
                logger.info(f"PDF '{input_pdf_path}' was successfully decrypted.")
            except Exception as e:
                logger.error(f"Failed to decrypt PDF '{input_pdf_path}': {e}")
                return  # Skip processing if decryption fails

        writer = PdfWriter()
        num_pages = len(reader.pages)
        logger.info(f"Pdf has '{num_pages}' pages")

        # Process the PDF page-by-page
        for page_number in range(1, num_pages + 1):
            if should_pause:
                should_pause()
            logger.info(f"Processing page {page_number} of {num_pages} for {input_pdf_path}")

            # Wait for resources to be available
            wait_for_resources()

            try:
                # Convert a single page to an image
                try:
                    print("Page Number: {}".format(page_number))
                    images = convert_from_path(
                        input_pdf_path,
                        first_page=page_number,
                        last_page=page_number,
                        dpi=200,
                        poppler_path=poppler_path
                    )
                except Exception as e:
                    logger.warning("Default DPI failed for page {}: {}".format(page_number, e))
                    logger.info(f"Retrying with lower DPI for page {page_number}...")
                    images = convert_from_path(
                        input_pdf_path,
                        first_page=page_number,
                        last_page=page_number,
                        dpi=100,
                        poppler_path=poppler_path
                    )

                if not images or len(images) == 0:
                    logger.warning(f"No images returned for page {page_number}. Skipping...")
                    writer.add_page(reader.pages[page_number-1])
                    # Save progress incrementally to avoid loss in case of failure
                    with open(output_pdf_path, "wb") as f_out:
                        writer.write(f_out)

                    continue

                image = images[0]  # There should be only one image for this page
                if len(images)>1:
                    logger.error("multiple images found")
                # Correct the orientation of the image
                corrected_image = correct_image_orientation(image)

                # Perform OCR on the corrected image
                pdf_bytes = image_to_pdf_or_hocr(corrected_image, extension='pdf', lang='deu')
                page_stream = BytesIO(pdf_bytes)
                ocr_page = PdfReader(page_stream)
                writer.add_page(ocr_page.pages[0])
                logger.info(f"OCR completed for page {page_number}.")

                # Save progress incrementally to avoid loss in case of failure
                with open(output_pdf_path, "wb") as f_out:
                    writer.write(f_out)
            except Exception as page_error:
                if getattr(page_error, "coordinator_pause_requested", False):
                    raise
                logger.error(f"Error processing page {page_number}: {page_error}", exc_info=True)

        logger.info(f"OCR completed successfully for: {input_pdf_path}")

    except Exception as e:
        if getattr(e, "coordinator_pause_requested", False):
            raise
        logger.error(f"Error in convert_pdf_to_searchable for {input_pdf_path}: {e}", exc_info=True)
        raise





def process_folder(input_folder, output_root):
    """
    Recursively processes a folder, converting all PDFs to searchable PDFs
    and replicating the folder structure under a new root.

    Args:
        input_folder (str): Path to the folder to scan.
        output_root (str): Root path of the converted folder structure.
    """
    logger.info(f"Starting to process folder: {input_folder}")
    try:
        for root, dirs, files in os.walk(input_folder):
            # Determine corresponding output folder path
            relative_path = os.path.relpath(root, input_folder)
            output_folder = os.path.join(output_root, relative_path)

            # Ensure the output folder exists
            os.makedirs(output_folder, exist_ok=True)

            for file in files:
                input_file_path = os.path.join(root, file)
                output_file_path = os.path.join(output_folder, file)

                if file.lower().endswith(".pdf"):
                    logger.info(f"Converting: {input_file_path} -> {output_file_path}")
                    # Convert PDF to searchable PDF
                    try:
                        convert_pdf_to_searchable(input_file_path, output_file_path)
                    except Exception as e:
                        logger.error(f"Error converting {input_file_path}: {e}")
                        traceback.print_exc()  # Print traceback for debugging
                else:
                    # Copy non-PDF files directly
                    logger.info(f"Copying non-PDF file: {input_file_path} -> {output_file_path}")
                    shutil.copy2(input_file_path, output_file_path)

        logger.info(f"Processing completed. Converted folder: {output_root}")

    except Exception as e:
        logger.error(f"Error processing folder {input_folder}: {e}")
        traceback.print_exc()  # Print traceback for debugging
        raise  # Re-raise the exception to let the caller know


# Example for testing the function directly
# convert_pdf_to_searchable("test.pdf", "ocr_test.pdf")
# process_folder("input_folder", "output_folder")



#convert_pdf_to_searchable("test.pdf", "ocr.pdf")
##if __name__ == "__main__":
##    input_folder = input("Enter the path to the input folder: ").strip()
##
##    # Define the output folder (renamed root folder as 'converted')
##    output_root = os.path.join(os.path.dirname(input_folder), "converted")
##
##    if not os.path.exists(input_folder):
##        print(f"Error: Input folder '{input_folder}' does not exist.")
##    else:
##        process_folder(input_folder, output_root)
##
