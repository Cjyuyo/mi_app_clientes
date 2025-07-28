import subprocess
import sys

# Step 1: Install required packages
try:
    print("Installing required packages (pandas, openpyxl)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "openpyxl"])
    print("Packages installed successfully.")
except Exception as e:
    print(f"Error installing packages: {e}")
    sys.exit(1)

# Step 2: Now that packages are installed, import pandas
import pandas as pd

# Step 3: Read the Excel file and list sheets
try:
    excel_file = 'clientes_actualizado_final.xlsx'
    print(f"\nReading Excel file: '{excel_file}'")
    xls = pd.ExcelFile(excel_file)
    print("Se encontraron las siguientes hojas en el archivo:")
    for sheet_name in xls.sheet_names:
        print(f"- '{sheet_name}'")
except FileNotFoundError:
    print(f"Error: No se encontró el archivo '{excel_file}'. Asegúrate de que está subido.")
except Exception as e:
    print(f"Ocurrió un error al leer el archivo: {e}")