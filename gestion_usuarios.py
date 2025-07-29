import os
import psycopg2
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash
from getpass import getpass
import argparse

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def crear_usuario(args):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM administradores WHERE usuario = %s;", (args.usuario,))
            if cur.fetchone():
                print(f"❌ ERROR: El usuario '{args.usuario}' ya existe.")
                return

            print(f"Creando nuevo usuario: {args.usuario}")
            password = getpass(f"Introduce la contraseña para {args.usuario}: ")
            if password != getpass("Confirma la contraseña: "):
                print("❌ ERROR: Las contraseñas no coinciden.")
                return

            password_hash = generate_password_hash(password)
            cur.execute(
                """
                INSERT INTO administradores (usuario, password_hash, rol, nombre_completo, estatus)
                VALUES (%s, %s, %s, %s, 'activo');
                """,
                (args.usuario, password_hash, args.rol, args.nombre)
            )
            conn.commit()
            print(f"✅ ¡Usuario '{args.usuario}' creado exitosamente con el rol '{args.rol}'!")
    finally:
        conn.close()

def listar_usuarios(args):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, usuario, rol, nombre_completo, estatus, fecha_creacion FROM administradores ORDER BY id;")
            usuarios = cur.fetchall()
            print("\n--- Lista de Usuarios del Sistema ---")
            for u in usuarios:
                print(f"ID: {u[0]}, Usuario: {u[1]}, Rol: {u[2]}, Nombre: {u[3]}, Estatus: {u[4]}, Creado: {u[5].strftime('%Y-%m-%d')}")
            print("-------------------------------------\n")
    finally:
        conn.close()

def cambiar_nombre(args):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE administradores SET usuario = %s WHERE usuario = %s;", (args.nuevo_nombre, args.nombre_actual))
            if cur.rowcount == 0:
                print(f"❌ ERROR: No se encontró el usuario '{args.nombre_actual}'.")
            else:
                conn.commit()
                print(f"✅ ¡Éxito! El usuario '{args.nombre_actual}' fue renombrado a '{args.nuevo_nombre}'.")
    finally:
        conn.close()

# --- NUEVA FUNCIÓN ---
def cambiar_password(args):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            print(f"Cambiando contraseña para el usuario: {args.usuario}")
            password = getpass("Introduce la nueva contraseña: ")
            if password != getpass("Confirma la nueva contraseña: "):
                print("❌ ERROR: Las contraseñas no coinciden.")
                return
            
            password_hash = generate_password_hash(password)
            cur.execute("UPDATE administradores SET password_hash = %s WHERE usuario = %s;", (password_hash, args.usuario))
            
            if cur.rowcount == 0:
                print(f"❌ ERROR: No se encontró el usuario '{args.usuario}'.")
            else:
                conn.commit()
                print(f"✅ ¡Contraseña para '{args.usuario}' actualizada exitosamente!")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Herramienta para gestionar usuarios administradores.")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Comandos disponibles")

    p_crear = subparsers.add_parser("crear", help="Crea un nuevo usuario.")
    p_crear.add_argument("usuario", help="El nombre de usuario para el login.")
    p_crear.add_argument("rol", choices=['superadmin', 'gerente', 'administradora', 'asistente'], help="El rol del usuario.")
    p_crear.add_argument("nombre", help="El nombre completo (entre comillas si tiene espacios).")
    p_crear.set_defaults(func=crear_usuario)

    p_listar = subparsers.add_parser("listar", help="Muestra todos los usuarios.")
    p_listar.set_defaults(func=listar_usuarios)

    p_renombrar = subparsers.add_parser("renombrar", help="Cambia el nombre de un usuario.")
    p_renombrar.add_argument("nombre_actual", help="El nombre de usuario actual.")
    p_renombrar.add_argument("nuevo_nombre", help="El nuevo nombre de usuario.")
    p_renombrar.set_defaults(func=cambiar_nombre)
    
    # --- NUEVO COMANDO ---
    p_reset_pass = subparsers.add_parser("reset-pass", help="Restablece la contraseña de un usuario.")
    p_reset_pass.add_argument("usuario", help="El usuario al que se le cambiará la contraseña.")
    p_reset_pass.set_defaults(func=cambiar_password)
    
    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()