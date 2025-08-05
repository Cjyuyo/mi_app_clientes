import os
import psycopg2
from dotenv import load_dotenv

# =============================================================================
# --- CONFIGURACIÓN ---
# Cédulas a eliminar.
# =============================================================================
CEDULAS_A_ELIMINAR = [
    "17559595",
    "17881898",
    "20810454",
    "31356606"
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
                # Normalizar cédula para buscar con y sin prefijo 'V-'
                cedula_norm = cedula.replace('V-', '').replace('v-', '')
                print(f"\n procesando Cédula: {cedula_norm}...")
                
                # 1. Encontrar el ID del cliente
                cur.execute("SELECT id FROM clientes WHERE cedula = %s OR cedula = %s;", (cedula_norm, f'V-{cedula_norm}'))
                cliente_record = cur.fetchone()
                
                if not cliente_record:
                    print(f"   - 🟡 AVISO: No se encontró ningún cliente con la cédula {cedula_norm}. Saltando al siguiente.")
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
                
                print(f"   - ✅ ¡Limpieza completa para la cédula {cedula_norm}!")

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
    confirmacion = input(f"ADVERTENCIA: Estás a punto de eliminar permanentemente a {len(CEDULAS_A_ELIMINAR)} cliente(s) y todos sus datos asociados.\nEsta acción NO se puede deshacer.\n\nEscribe 'CONFIRMAR' para proceder: ")
    if confirmacion == "CONFIRMAR":
        limpiar_clientes_por_cedula(CEDULAS_A_ELIMINAR)
    else:
        print("\nOperación cancelada por el usuario.")