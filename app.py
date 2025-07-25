import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv

# Carga las variables de entorno desde el archivo .env
load_dotenv()

app = Flask(__name__)

# --- CONFIGURACIÓN DE LA BASE DE DATOS ---
# Render utiliza la variable de entorno DATABASE_URL para la conexión a PostgreSQL.
# Esta línea adapta el formato de la URL de conexión para que sea compatible con SQLAlchemy.
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'un-secreto-muy-seguro-por-defecto')

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# --- MODELOS DE LA BASE DE DATOS ---
# Define la estructura de la tabla 'cliente' en la base de datos.
class Cliente(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    cedula = db.Column(db.String(20), unique=True, nullable=False)
    telefono = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(100), nullable=True)
    direccion = db.Column(db.String(200), nullable=True)

    def __repr__(self):
        return f'<Cliente {self.nombre}>'

# --- RUTAS DE LA APLICACIÓN ---

@app.route('/')
def index():
    """
    Ruta principal que redirige a la página de consulta de clientes.
    """
    return redirect(url_for('consultar_clientes'))

@app.route('/clientes')
def consultar_clientes():
    """
    Muestra la lista de todos los clientes con paginación.
    Permite buscar clientes por su número de cédula de forma exacta.
    """
    query = request.args.get('query')
    page = request.args.get('page', 1, type=int)
    per_page = 15  # Define cuántos clientes se muestran por página

    if query:
        # --- MODIFICACIÓN REALIZADA ---
        # Se ha cambiado el método de búsqueda para que sea una coincidencia exacta.
        # El código anterior usaba .ilike(f'%{query}%') para búsquedas parciales.
        # Ahora, se usa '==' para asegurar que la cédula ingresada sea idéntica a la registrada.
        pagination = Cliente.query.filter(Cliente.cedula == query).paginate(page=page, per_page=per_page, error_out=False)
    else:
        # Si no hay búsqueda, muestra todos los clientes ordenados por el más reciente.
        pagination = Cliente.query.order_by(Cliente.id.desc()).paginate(page=page, per_page=per_page, error_out=False)

    clientes = pagination.items
    return render_template('clientes.html', clientes=clientes, pagination=pagination, query=query)

@app.route('/cliente/nuevo', methods=['GET', 'POST'])
def agregar_cliente():
    """
    Maneja la creación de un nuevo cliente a través de un formulario.
    """
    if request.method == 'POST':
        nombre = request.form['nombre']
        cedula = request.form['cedula']
        telefono = request.form['telefono']
        email = request.form['email']
        direccion = request.form['direccion']

        # Verifica que la cédula no esté ya registrada para evitar duplicados.
        if Cliente.query.filter_by(cedula=cedula).first():
            flash(f'El cliente con cédula {cedula} ya existe.', 'danger')
            return redirect(url_for('agregar_cliente'))

        nuevo_cliente = Cliente(
            nombre=nombre,
            cedula=cedula,
            telefono=telefono,
            email=email,
            direccion=direccion
        )
        db.session.add(nuevo_cliente)
        db.session.commit()
        flash('Cliente agregado exitosamente!', 'success')
        return redirect(url_for('consultar_clientes'))

    return render_template('agregar_cliente.html')

@app.route('/cliente/editar/<int:id>', methods=['GET', 'POST'])
def editar_cliente(id):
    """
    Permite editar los datos de un cliente existente.
    """
    cliente = Cliente.query.get_or_404(id)
    if request.method == 'POST':
        nueva_cedula = request.form['cedula']
        # Verifica que la nueva cédula no pertenezca a otro cliente.
        if nueva_cedula != cliente.cedula and Cliente.query.filter_by(cedula=nueva_cedula).first():
            flash(f'La cédula {nueva_cedula} ya está registrada para otro cliente.', 'danger')
            return render_template('editar_cliente.html', cliente=cliente)

        cliente.nombre = request.form['nombre']
        cliente.cedula = nueva_cedula
        cliente.telefono = request.form['telefono']
        cliente.email = request.form['email']
        cliente.direccion = request.form['direccion']
        db.session.commit()
        flash('Cliente actualizado exitosamente!', 'success')
        return redirect(url_for('consultar_clientes'))

    return render_template('editar_cliente.html', cliente=cliente)

@app.route('/cliente/eliminar/<int:id>', methods=['POST'])
def eliminar_cliente(id):
    """
    Elimina un cliente de la base de datos.
    """
    cliente = Cliente.query.get_or_404(id)
    db.session.delete(cliente)
    db.session.commit()
    flash('Cliente eliminado exitosamente.', 'success')
    return redirect(url_for('consultar_clientes'))

# Punto de entrada para la ejecución (no se usará en producción en Render).
if __name__ == '__main__':
    app.run(debug=False)
