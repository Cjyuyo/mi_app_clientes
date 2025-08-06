import os
import psycopg2
from dotenv import load_dotenv

def limpiar_registros_de_prueba_v2():
    """
    Elimina de forma completa y en cascada todos los clientes y sus datos asociados
    que fueron creados a través de la aplicación.
    
    Identifica los registros de prueba si cumplen CUALQUIERA de estas condiciones:
    1. Tienen una firma digital (registros nuevos).
    2. Tienen una entrada en la tabla 'caja_inscripciones' (registros antiguos y nuevos).
    
    Esto asegura que NO se afecten los datos migrados desde Excel.
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
            
            # 1. Identificar a TODOS los clientes de prueba usando la lógica mejorada
            print("Buscando clientes de prueba registrados a través de la aplicación (método robusto)...")
            cur.execute("""
                SELECT DISTINCT c.id, c.nombre, c.apellido, c.cedula
                FROM clientes c
                LEFT JOIN caja_inscripciones ci ON c.id = ci.cliente_id
                WHERE c.firma_digital IS NOT NULL OR ci.id IS NOT NULL;
            """)
            clientes_a_eliminar = cur.fetchall()
            
            if not clientes_a_eliminar:
                print("\n✅ No se encontraron registros de prueba para eliminar. La base de datos está limpia.")
                return

            print(f"\nSe encontraron {len(clientes_a_eliminar)} clientes de prueba para eliminar:")
            for cliente in clientes_a_eliminar:
                print(f"  - ID: {cliente[0]}, Nombre: {cliente[1]} {cliente[2]}, C.I: {cliente[3]}")

            # 2. Confirmación del usuario
            confirmacion = input("\nADVERTENCIA: Estás a punto de eliminar permanentemente estos clientes y TODOS sus datos asociados (pagos, comisiones, etc.).\nEsta acción NO se puede deshacer.\n\nEscribe 'CONFIRMAR' para proceder: ")
            if confirmacion != "CONFIRMAR":
                print("\nOperación cancelada por el usuario.")
                return

            # 3. Proceder con la eliminación en cascada
            print("\nIniciando limpieza completa...")
            ids_a_eliminar = [cliente[0] for cliente in clientes_a_eliminar]
            
            # Eliminar de tablas relacionadas en orden de dependencia para evitar errores
            tablas_relacionadas = [
                "gestiones_cobranza",
                "ofertas",
                "comisiones_generadas",
                "caja_inscripciones",
                "pagos"
            ]
            
            for tabla in tablas_relacionadas:
                print(f"  - Limpiando de la tabla '{tabla}'...")
                cur.execute(f"DELETE FROM {tabla} WHERE cliente_id = ANY(%s);", (ids_a_eliminar,))
                print(f"    {cur.rowcount} registros eliminados.")

            # Finalmente, eliminar los registros principales de los clientes
            print("  - Limpiando de la tabla 'clientes'...")
            cur.execute("DELETE FROM clientes WHERE id = ANY(%s);", (ids_a_eliminar,))
            print(f"    {cur.rowcount} registros eliminados.")
            
            # Confirmar todos los cambios en la base de datos
            conn.commit()
            print("\n\n🎉 ¡PROCESO FINALIZADO! Todos los registros de prueba han sido eliminados exitosamente.")

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
    limpiar_registros_de_prueba_v2()
