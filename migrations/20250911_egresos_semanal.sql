-- migrations/20250911c_egresos_tipo_y_refs.sql
BEGIN;

-- Asegura columna 'tipo' en egresos_planificados (algunos entornos no la tenían)
ALTER TABLE egresos_planificados
  ADD COLUMN IF NOT EXISTS tipo VARCHAR(20);
UPDATE egresos_planificados SET tipo = COALESCE(NULLIF(tipo,''), 'Fijo') WHERE tipo IS NULL;

-- Crea tablas de ocurrencias/pagos si no existen (idempotente)
CREATE TABLE IF NOT EXISTS egresos_ocurrencias (
  id SERIAL PRIMARY KEY,
  egreso_id INT NOT NULL REFERENCES egresos_planificados(id) ON DELETE CASCADE,
  fecha_programada DATE NOT NULL,
  periodo_tipo TEXT CHECK (periodo_tipo IN ('mensual','semanal')) NOT NULL DEFAULT 'mensual',
  periodo_clave TEXT NOT NULL,
  monto_programado_usd NUMERIC(12,2) NOT NULL DEFAULT 0,
  monto_pagado_usd NUMERIC(12,2) NOT NULL DEFAULT 0,
  estado TEXT CHECK (estado IN ('pendiente','parcial','pagado')) NOT NULL DEFAULT 'pendiente',
  UNIQUE (egreso_id, periodo_tipo, periodo_clave, fecha_programada)
);
CREATE INDEX IF NOT EXISTS idx_eocc_periodo ON egresos_ocurrencias (periodo_tipo, periodo_clave);
CREATE INDEX IF NOT EXISTS idx_eocc_estado  ON egresos_ocurrencias (estado);

CREATE TABLE IF NOT EXISTS egresos_pagos (
  id SERIAL PRIMARY KEY,
  egreso_ocurrencia_id INT NOT NULL REFERENCES egresos_ocurrencias(id) ON DELETE CASCADE,
  movimiento_tesoreria_id INT,
  monto_original NUMERIC(14,2) NOT NULL,
  moneda TEXT CHECK (moneda IN ('USD','VES','USDT')) NOT NULL DEFAULT 'USD',
  tasa_aplicada NUMERIC(14,6),
  monto_equivalente_usd NUMERIC(14,2) NOT NULL DEFAULT 0,
  fecha_pago TIMESTAMP NOT NULL DEFAULT NOW(),
  nota TEXT
);
CREATE INDEX IF NOT EXISTS idx_epagos_occ   ON egresos_pagos (egreso_ocurrencia_id);
CREATE INDEX IF NOT EXISTS idx_epagos_fecha ON egresos_pagos (fecha_pago);

-- Añade referencias en operaciones_tesoreria (si existe)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = ANY (current_schemas(true)) AND table_name = 'operaciones_tesoreria'
  ) THEN
    ALTER TABLE operaciones_tesoreria
      ADD COLUMN IF NOT EXISTS referencia_tipo TEXT,
      ADD COLUMN IF NOT EXISTS referencia_id   INT;
    CREATE INDEX IF NOT EXISTS idx_ot_ref ON operaciones_tesoreria (referencia_tipo, referencia_id);
  END IF;
END $$;

COMMIT;
