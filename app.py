import os
from flask import Flask, render_template

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'una-clave-secreta-por-defecto-para-desarrollo')

# --- RUTAS DE PRUEBA SIMPLES ---

@app.route('/')
def index():
    # Esta ruta debería mostrar la página de registro normal.
    # Si esto funciona, el problema no es el renderizado de plantillas.
    try:
        return render_template('index.html')
    except Exception as e:
        return f"<h1>Error al cargar la plantilla index.html</h1><p>{e}</p>"

@app.route('/consulta')
def consulta():
    # Esta ruta devuelve un texto simple para probar el enrutamiento.
    return "<h1>Página de Consulta de Prueba</h1><p>Si ves esto, la ruta /consulta funciona.</p>"

@app.route('/recibo/<int:pago_id>')
def ver_recibo(pago_id):
    # Esta ruta prueba una URL dinámica.
    return f"<h1>Viendo el Recibo de Prueba N° {pago_id}</h1>"

@app.route('/registrar_pago/<int:client_id>')
def registrar_pago(client_id):
    return f"<h1>Registrando Pago de Prueba para Cliente N° {client_id}</h1>"

# --- INICIO DE LA APP ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

