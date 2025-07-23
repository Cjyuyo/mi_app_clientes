-- Se usa CASCADE para borrar las tablas y sus dependencias si existen
DROP TABLE IF EXISTS clientes CASCADE;
DROP TABLE IF EXISTS pagos CASCADE;

-- Crea la tabla de clientes
CREATE TABLE clientes (
  id SERIAL PRIMARY KEY,
  cedula TEXT UNIQUE,
  contrato_nro TEXT,
  nombre_apellido TEXT,
  telefono TEXT,
  fecha_ingreso TEXT,
  grupo TEXT,
  plan TEXT,
  moneda_pago TEXT,
  asesor TEXT,
  responsable TEXT,
  proceso TEXT,
  estatus TEXT,
  estatus_1 TEXT,
  inscripcion_porcentaje REAL,
  inscripcion_monto REAL,
  cuotas_pagas TEXT,
  pagos_impuntuales TEXT,
  cuotas_mora TEXT,
  valor_cuota REAL,
  fecha_pago_recurrente TEXT,
  estatus_cuota TEXT,
  valor_cancelado REAL,
  observacion TEXT,
  fecha_inscripcion TEXT,
  plan_contratado TEXT,
  duracion_plan TEXT,
  reserva_monto_total REAL,
  reserva_monto_pagado REAL
);

-- Crea la tabla de pagos, vinculada al ID del cliente
CREATE TABLE pagos (
  id SERIAL PRIMARY KEY,
  -- CORRECCIÓN: Se vincula el pago al ID único del cliente
  cliente_id INTEGER NOT NULL,
  monto REAL NOT NULL,
  cuotas INTEGER NOT NULL,
  recibo TEXT NOT NULL,
  forma_pago TEXT NOT NULL,
  fecha_pago TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (cliente_id) REFERENCES clientes (id) ON DELETE CASCADE
);