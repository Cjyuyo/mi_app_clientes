import os
import psycopg2
from dotenv import load_dotenv

def limpiar_solo_comisiones_de_prueba():
    """
    Elimina únicamente las comisiones generadas por los registros de prueba,
    dejando intactos los clientes y sus pagos para mantener un balance de referencia.
    
    Identifica los registros de prueba si cumplen CUALQUIERA de estas condiciones:
    1. Tienen una firma digital (registros nuevos).
    2. Tienen una entrada en la tabla 'caja_inscripciones' (registros antiguos y nuevos).
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
            
            # 1. Identificar a los clientes de prueba para saber qué comisiones borrar
            print("Buscando clientes de prueba para identificar sus comisiones asociadas...")
            cur.execute("""
                SELECT DISTINCT c.id, c.nombre, c.apellido, c.cedula
                FROM clientes c
                LEFT JOIN caja_inscripciones ci ON c.id = ci.cliente_id
                WHERE c.firma_digital IS NOT NULL OR ci.id IS NOT NULL;
            """)
            clientes_de_prueba = cur.fetchall()
            
            if not clientes_de_prueba:
                print("\n✅ No se encontraron clientes de prueba, por lo tanto no hay comisiones que limpiar.")
                return

            print(f"\nSe limpiarán las comisiones asociadas a los siguientes {len(clientes_de_prueba)} clientes de prueba:")
            for cliente in clientes_de_prueba:
                print(f"  - ID: {cliente[0]}, Nombre: {cliente[1]} {cliente[2]}")

            # 2. Confirmación del usuario
            confirmacion = input("\nADVERTENCIA: Estás a punto de eliminar permanentemente SOLO las comisiones generadas por estos clientes.\nLos clientes y sus pagos NO serán eliminados.\n\nEscribe 'CONFIRMAR' para proceder: ")
            if confirmacion != "CONFIRMAR":
                print("\nOperación cancelada por el usuario.")
                return

            # 3. Proceder con la eliminación de las comisiones
            print("\nIniciando limpieza de comisiones...")
            ids_clientes_prueba = [cliente[0] for cliente in clientes_de_prueba]
            
            cur.execute(f"DELETE FROM comisiones_generadas WHERE cliente_id = ANY(%s);", (ids_clientes_prueba,))
            print(f"  - {cur.rowcount} registros de comisiones eliminados.")

            # Confirmar los cambios en la base de datos
            conn.commit()
            print("\n\n🎉 ¡PROCESO FINALIZADO! Todas las comisiones de prueba han sido eliminadas exitosamente.")

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
    limpiar_solo_comisiones_de_prueba()
