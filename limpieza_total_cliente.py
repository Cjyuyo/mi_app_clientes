import os
import psycopg2
from dotenv import load_dotenv

# =============================================================================
# --- CONFIGURACIÓN ---
# Agrega aquí las cédulas de los clientes que quieres eliminar por completo.
# =============================================================================
CEDULAS_A_ELIMINAR = [
    "V12345678",  # EJEMPLO 1: Reemplaza con la primera cédula
    "V87654321",  # EJEMPLO 2: Reemplaza con la segunda cédula
    "E99999999"   # EJEMPLO 3: Reemplaza con la tercera cédula
    # Puedes agregar más cédulas a la lista si es necesario
]
# =============================================================================

def limpiar_clientes_por_cedula(cedulas):
    """
    Elimina de forma completa y en cascada todos los registros asociados a una lista de cédulas.
    """
    load_dotenv()
    DATABASE_URL = os.getenv('DATABASE_URL')
    if not DATABASE_URL:
        print("❌ ERROR: La variable de entorno DATABASE_URL no está configurada.")
        return

    conn = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as cur:
            
            for cedula in cedulas:
                print(f"\n procesando Cédula: {cedula}...")
                
                # 1. Encontrar el ID del cliente
                cur.execute("SELECT id FROM clientes WHERE cedula = %s;", (cedula,))
                cliente_record = cur.fetchone()
                
                if not cliente_record:
                    print(f"   - 🟡 AVISO: No se encontró ningún cliente con la cédula {cedula}. Saltando al siguiente.")
                    continue
                
                cliente_id = cliente_record[0]
                print(f"   - Cliente encontrado con ID: {cliente_id}. Procediendo con la limpieza...")
                
                # 2. Eliminar registros de todas las tablas relacionadas (en orden de dependencia)
                tablas_relacionadas = [
                    "gestiones_cobranza",
                    "ofertas",
                    "comisiones_generadas",
                    "caja_inscripciones",
                    "pagos"
                ]
                
                for tabla in tablas_relacionadas:
                    print(f"     - Limpiando de la tabla '{tabla}'...")
                    cur.execute(f"DELETE FROM {tabla} WHERE cliente_id = %s;", (cliente_id,))
                    print(f"       {cur.rowcount} registros eliminados.")

                # 3. Finalmente, eliminar el registro principal del cliente
                print(f"     - Limpiando de la tabla 'clientes'...")
                cur.execute("DELETE FROM clientes WHERE id = %s;", (cliente_id,))
                print(f"       {cur.rowcount} registros eliminados.")
                
                print(f"   - ✅ ¡Limpieza completa para la cédula {cedula}!")

            # Si todo sale bien, confirmar todos los cambios.
            conn.commit()
            print("\n\n🎉 ¡PROCESO FINALIZADO! Todos los cambios han sido guardados en la base de datos.")

    except psycopg2.Error as e:
        print(f"\n\n❌ ERROR CRÍTICO: Se ha producido un error en la base de datos. Se revertirán todos los cambios.")
        print(f"   Detalle del error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
            print("   Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    if not CEDULAS_A_ELIMINAR or CEDULAS_A_ELIMINAR[0].startswith("V123"):
        print("====================================================================")
        print("  AVISO: Por favor, edita este script (`limpieza_total_cliente.py`)")
        print("         y añade las cédulas que deseas eliminar en la lista")
        print("         `CEDULAS_A_ELIMINAR` antes de ejecutarlo.")
        print("====================================================================")
    else:
        confirmacion = input(f"ADVERTENCIA: Estás a punto de eliminar permanentemente a {len(CEDULAS_A_ELIMINAR)} cliente(s) y todos sus datos asociados.\nEsta acción NO se puede deshacer.\n\nEscribe 'CONFIRMAR' para proceder: ")
        if confirmacion == "CONFIRMAR":
            limpiar_clientes_por_cedula(CEDULAS_A_ELIMINAR)
        else:
            print("\nOperación cancelada por el usuario.")