import os
import psycopg2
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash
from getpass import getpass

def crear_primer_admin():
    """
    Crea el primer usuario administrador en la base de datos con una contraseña segura.
    El script solicita la contraseña de forma interactiva para no exponerla.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    # --- DATOS DEL PRIMER ADMINISTRADOR ---
    usuario = "admin"
    rol = "superadmin"
    # ------------------------------------

    conn = None
    try:
        # Verificar si el usuario ya existe
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM administradores WHERE usuario = %s;", (usuario,))
            if cur.fetchone():
                print(f"El usuario '{usuario}' ya existe. No se tomarán acciones.")
                return

        # Si no existe, solicitar contraseña y crear
        print(f"Creando el primer usuario administrador: '{usuario}'")
        print("Por favor, introduce una contraseña segura para este usuario.")
        password = getpass("Contraseña: ")
        password_confirm = getpass("Confirma la contraseña: ")

        if password != password_confirm:
            print("\n❌ Las contraseñas no coinciden. Operación cancelada.")
            return

        if not password:
            print("\n❌ La contraseña no puede estar vacía. Operación cancelada.")
            return

        # Generar hash de la contraseña
        password_hash = generate_password_hash(password)

        # Insertar en la base de datos
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO administradores (usuario, password_hash, rol) VALUES (%s, %s, %s);",
                (usuario, password_hash, rol)
            )
            conn.commit()
            print(f"\n✅ ¡Éxito! El usuario '{usuario}' fue creado correctamente.")

    except psycopg2.Error as e:
        print(f"\n❌ ERROR de base de datos: {e}")
        if conn: conn.rollback()
    except Exception as e:
        print(f"\n❌ ERROR inesperado: {e}")
        if conn: conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    crear_primer_admin()