import pandas as pd
import openpyxl

try:
    excel_file = 'clientes_actualizado_final.xlsx'
    xls = pd.ExcelFile(excel_file)
    print("Se encontraron las siguientes hojas en el archivo:")
    for sheet_name in xls.sheet_names:
        print(f"- '{sheet_name}'")
except FileNotFoundError:
    print(f"Error: No se encontró el archivo '{excel_file}'. Asegúrate de que está subido.")
except Exception as e:
    print(f"Ocurrió un error al leer el archivo: {e}")