from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext, ROUND_HALF_EVEN
from typing import Dict, Any, List, Tuple, TypedDict, Optional


# -------- Decimal config (determinista) --------
getcontext().prec = 28
getcontext().rounding = ROUND_HALF_EVEN

D = Decimal


def _d(x: Any) -> Decimal:
    """Convierte a Decimal con seguridad (None -> 0)."""
    if x is None:
        return D("0")
    if isinstance(x, Decimal):
        return x
    return D(str(x))


def _q_usd(x: Decimal) -> Decimal:
    """Cuantiza a 2 decimales (USD) con redondeo bancario."""
    return x.quantize(D("0.01"))


def _usd_eq_from_eur(eur: Decimal, eur_bcv: Decimal, usd_bcv: Decimal) -> Decimal:
    """
    Convierte EUR a USD-equivalente usando tasas BCV:
      1 EUR en USD = (eur_bcv / usd_bcv)
    """
    if usd_bcv <= 0:
        raise ValueError("usd_bcv debe ser > 0 para convertir EUR a USD.")
    if eur_bcv <= 0:
        raise ValueError("eur_bcv debe ser > 0 para convertir EUR a USD.")
    ratio = eur_bcv / usd_bcv
    return eur * ratio


@dataclass(frozen=True)
class Buckets:
    """Buckets de ingresos expresados en USD o USD-equivalente."""
    efectivo_usd: Decimal
    euro_usd: Decimal
    usdt_usd: Decimal
    nequi_usd: Decimal
    bs_sc_usd_eq: Decimal
    bs_cc_usd_eq: Decimal

    @property
    def total_usd_eq(self) -> Decimal:
        return (
            self.efectivo_usd
            + self.euro_usd
            + self.usdt_usd
            + self.nequi_usd
            + self.bs_sc_usd_eq
            + self.bs_cc_usd_eq
        )


class EscenarioSalida(TypedDict):
    ingresos_por_bucket: Dict[str, str]
    egresos_divisas_directo: List[Dict[str, str]]
    egresos_divisas_225_247: List[Dict[str, str]]
    egresos_bs_directo: List[Dict[str, str]]
    deficit_usd_cubierto_en_bs: str
    penalidad_225_247_usd_eq: str
    penalidad_225_247_bs: str          # NUEVO: penalidad en Bs
    usd_vendidos_por_carril: str       # NUEVO: USD canalizados por carril
    colchon_por_bucket: Dict[str, str]
    colchon_total: str
    saldos: Dict[str, str]


class CarrilItem(TypedDict, total=False):
    """Ítem individual del carril 225→247 (parametrizable por mes y concepto)."""
    concepto: str
    monto_usd: float | str
    venta: float | str
    recompra: float | str


def _build_buckets(ing: Dict[str, Any], tasas: Dict[str, float]) -> Buckets:
    usd_bcv = _d(tasas.get("usd_bcv", 0))
    eur_bcv = _d(tasas.get("eur_bcv", 0))
    if usd_bcv <= 0:
        raise ValueError("La tasa usd_bcv debe ser > 0.")
    if eur_bcv <= 0:
        raise ValueError("La tasa eur_bcv debe ser > 0.")

    efectivo = _d(ing.get("efectivo", 0))
    eur = _d(ing.get("euro", 0))
    usdt = _d(ing.get("usdt", 0))
    nequi = _d(ing.get("nequi", 0))
    bs_sc = _d(ing.get("bs_sc", 0))  # Bs S/C
    bs_cc = _d(ing.get("bs_cc", 0))  # Bs C/C

    euro_usd = _usd_eq_from_eur(eur, eur_bcv=eur_bcv, usd_bcv=usd_bcv)
    bs_sc_usd = bs_sc / usd_bcv
    bs_cc_usd = bs_cc / usd_bcv

    return Buckets(
        efectivo_usd=_q_usd(efectivo),
        euro_usd=_q_usd(euro_usd),
        usdt_usd=_q_usd(usdt),
        nequi_usd=_q_usd(nequi),
        bs_sc_usd_eq=_q_usd(bs_sc_usd),
        bs_cc_usd_eq=_q_usd(bs_cc_usd),
    )


def _sum_buckets(a: Buckets, b: Buckets) -> Buckets:
    return Buckets(
        efectivo_usd=_q_usd(a.efectivo_usd + b.efectivo_usd),
        euro_usd=_q_usd(a.euro_usd + b.euro_usd),
        usdt_usd=_q_usd(a.usdt_usd + b.usdt_usd),
        nequi_usd=_q_usd(a.nequi_usd + b.nequi_usd),
        bs_sc_usd_eq=_q_usd(a.bs_sc_usd_eq + b.bs_sc_usd_eq),
        bs_cc_usd_eq=_q_usd(a.bs_cc_usd_eq + b.bs_cc_usd_eq),
    )


def _empty_buckets() -> Buckets:
    z = _q_usd(D("0"))
    return Buckets(z, z, z, z, z, z)


def _colchon_por_bucket(b: Buckets, colchon_pct: Decimal) -> Dict[str, Decimal]:
    return {
        "efectivo_usd": _q_usd(b.efectivo_usd * colchon_pct),
        "euro_usd": _q_usd(b.euro_usd * colchon_pct),
        "usdt_usd": _q_usd(b.usdt_usd * colchon_pct),
        "nequi_usd": _q_usd(b.nequi_usd * colchon_pct),
        "bs_sc_usd_eq": _q_usd(b.bs_sc_usd_eq * colchon_pct),
        "bs_cc_usd_eq": _q_usd(b.bs_cc_usd_eq * colchon_pct),
    }


def _format_money(x: Decimal) -> str:
    return f"{_q_usd(x)}"


def _classifica_egresos(
    egresos_divisas: Dict[str, Any],
    bloque_directo_keys: List[str],
    pagan_por_225_247_keys: List[str],
) -> Tuple[Dict[str, Decimal], Dict[str, Decimal], Dict[str, Decimal]]:
    """
    Separa egresos en USD en:
      - directo (bloque_directo_keys),
      - 225_247 (pagan_por_225_247_keys),
      - otros (cualquier USD no clasificado: por defecto, directos).
    """
    directo: Dict[str, Decimal] = {}
    c_225_247: Dict[str, Decimal] = {}
    otros: Dict[str, Decimal] = {}

    for k, v in (egresos_divisas or {}).items():
        amt = _q_usd(_d(v))
        if k in set(bloque_directo_keys or []):
            directo[k] = amt
        elif k in set(pagan_por_225_247_keys or []):
            c_225_247[k] = amt
        else:
            otros[k] = amt
    return directo, c_225_247, otros


def _aplica_egresos_y_penalidad(
    buckets: Buckets,
    egresos: Dict[str, Any],
    tasas: Dict[str, float],
    params: Dict[str, Any],
) -> Tuple[
    Buckets,
    List[Dict[str, str]],
    List[Dict[str, str]],
    List[Dict[str, str]],
    Decimal,
    Decimal,
    Decimal,  # NUEVO: penalidad_225_247_bs
    Decimal,  # NUEVO: usd_vendidos_por_carril
]:
    """
    Aplica:
      1) Egresos en USD - bloque directo.
      2) Egresos en USD - carril 225→247 **SOLO MODO MANUAL** (por ítem con venta/recompra por fila de mes).
      3) Egresos en Bs directos.
      4) Déficit USD del bloque directo cubierto con Bs.
      5) Penalidad restada del bucket Bs.
    Retorna:
      - buckets_finales
      - detalle_directo_usd
      - detalle_225_247
      - detalle_bs
      - deficit_usd_cubierto_en_bs (USD)
      - penalidad_225_247_usd_eq (USD-eq)
      - penalidad_225_247_bs (Bs)
      - usd_vendidos_por_carril (USD)
    """
    usd_bcv = _d(tasas.get("usd_bcv", 0))
    if usd_bcv <= 0:
        raise ValueError("La tasa usd_bcv debe ser > 0.")

    # Aunque recibimos llaves/tasas globales en params/tasas, en modo manual no se usan.
    bloque_directo_keys = list(params.get("bloque_directo_keys", []))
    # pagan_225_247_keys = list(params.get("pagan_por_225_247_keys", []))  # NO usado en modo manual

    # 1) Clasifica egresos USD (directo / otros) — el carril no se toma por claves en modo manual.
    egresos_divisas: Dict[str, Any] = egresos.get("divisas") or {}
    dir_usd, _by225_por_clave, otros_usd = _classifica_egresos(
        egresos_divisas, bloque_directo_keys, []  # ignoramos llaves de carril
    )
    # Otros USD no clasificados -> directo
    for k, v in otros_usd.items():
        dir_usd[k] = v

    # 2) Egresos Bs directos (USD-eq)
    bs_bcv = _q_usd(_d(egresos.get("bs_bcv", 0)))
    bs_euro_bcv = _q_usd(_d(egresos.get("bs_euro_bcv", 0)))
    variables_bs = egresos.get("variables_bs") or {}
    devoluciones_bs = _q_usd(_d(variables_bs.get("devoluciones_bs", 0)))
    registro_bs = _q_usd(_d(variables_bs.get("registro", 0)))

    # 3) variables_usd -> bloque directo USD
    variables_usd = egresos.get("variables_usd") or {}
    devoluciones_usd = _q_usd(_d(variables_usd.get("devoluciones_usd", 0)))
    if devoluciones_usd > 0:
        dir_usd["devoluciones_usd"] = dir_usd.get("devoluciones_usd", D("0.00")) + devoluciones_usd

    # ---------- Directo USD ----------
    usd_pool = buckets.efectivo_usd + buckets.euro_usd + buckets.usdt_usd + buckets.nequi_usd
    total_directo = sum(dir_usd.values(), D("0.00"))
    detalle_directo: List[Dict[str, str]] = []

    if total_directo > 0:
        if usd_pool >= total_directo:
            restante = total_directo
            mutable = {
                "efectivo_usd": buckets.efectivo_usd,
                "euro_usd": buckets.euro_usd,
                "usdt_usd": buckets.usdt_usd,
                "nequi_usd": buckets.nequi_usd,
            }
            for key in ("efectivo_usd", "euro_usd", "usdt_usd", "nequi_usd"):
                if restante <= 0:
                    break
                take = min(mutable[key], restante)
                mutable[key] = _q_usd(mutable[key] - take)
                restante = _q_usd(restante - take)
            buckets = Buckets(
                efectivo_usd=mutable["efectivo_usd"],
                euro_usd=mutable["euro_usd"],
                usdt_usd=mutable["usdt_usd"],
                nequi_usd=mutable["nequi_usd"],
                bs_sc_usd_eq=buckets.bs_sc_usd_eq,
                bs_cc_usd_eq=buckets.bs_cc_usd_eq,
            )
            deficit_cubierto = D("0.00")
        else:
            deficit = _q_usd(total_directo - usd_pool)
            # Consumimos todo el USD disponible:
            buckets = Buckets(
                efectivo_usd=D("0.00"),
                euro_usd=D("0.00"),
                usdt_usd=D("0.00"),
                nequi_usd=D("0.00"),
                bs_sc_usd_eq=buckets.bs_sc_usd_eq,
                bs_cc_usd_eq=buckets.bs_cc_usd_eq,
            )
            # Cubrimos déficit con Bs (USD-eq) proporcionalmente entre SC y CC
            bs_total = buckets.bs_sc_usd_eq + buckets.bs_cc_usd_eq
            used = min(bs_total, deficit)
            if bs_total > 0:
                ratio_sc = buckets.bs_sc_usd_eq / bs_total
                use_sc = _q_usd(used * ratio_sc)
                use_cc = _q_usd(used - use_sc)
            else:
                use_sc = D("0.00"); use_cc = D("0.00")
            buckets = Buckets(
                efectivo_usd=buckets.efectivo_usd,
                euro_usd=buckets.euro_usd,
                usdt_usd=buckets.usdt_usd,
                nequi_usd=buckets.nequi_usd,
                bs_sc_usd_eq=_q_usd(buckets.bs_sc_usd_eq - use_sc),
                bs_cc_usd_eq=_q_usd(buckets.bs_cc_usd_eq - use_cc),
            )
            deficit_cubierto = used
    else:
        deficit_cubierto = D("0.00")

    for k, v in dir_usd.items():
        detalle_directo.append({"concepto": k, "monto_usd": _format_money(v)})

    # ---------- Carril 225→247 SOLO MANUAL ----------
    detalle_225: List[Dict[str, str]] = []
    penalidad_bs_total = D("0.00")     # penalidad en Bs
    penalidad_usd_eq_total = D("0.00") # penalidad en USD-eq
    usd_vendidos_total = D("0.00")     # USD canalizados por el carril

    items: Optional[List[CarrilItem]] = egresos.get("carril_225_247_items")
    if items:
        for it in items:
            concepto = str(it.get("concepto", "carril_225_247"))
            monto = _q_usd(_d(it.get("monto_usd", 0)))
            venta = _d(it.get("venta", 0))
            recompra = _d(it.get("recompra", 0))
            if venta <= 0 or recompra <= 0:
                raise ValueError(f"Tasas inválidas en ítem {concepto}: venta/recompra deben ser > 0.")
            if venta > recompra:
                raise ValueError(f"Inconsistencia en ítem {concepto}: venta_usd no puede ser mayor que recompra_usd.")

            # Penalidad en Bs (∆tasas × USD vendidos) y su equivalente en USD
            pen_bs = _q_usd((recompra - venta) * monto)  # Bs
            pen_usd_eq = _q_usd(pen_bs / usd_bcv)        # USD-eq

            penalidad_bs_total = _q_usd(penalidad_bs_total + pen_bs)
            penalidad_usd_eq_total = _q_usd(penalidad_usd_eq_total + pen_usd_eq)
            usd_vendidos_total = _q_usd(usd_vendidos_total + monto)

            detalle_225.append({
                "concepto": concepto,
                "monto_usd": _format_money(monto),
                "venta": _format_money(venta),
                "recompra": _format_money(recompra),
                "penalidad_bs": _format_money(pen_bs),
                "penalidad_usd_eq": _format_money(pen_usd_eq),
            })

        # Restar penalidad (en USD-eq) del bucket Bs, prorrateado SC/CC
        if penalidad_usd_eq_total > 0:
            bs_total = buckets.bs_sc_usd_eq + buckets.bs_cc_usd_eq
            if bs_total > 0:
                ratio_sc = buckets.bs_sc_usd_eq / bs_total
                use_sc = _q_usd(penalidad_usd_eq_total * ratio_sc)
                use_cc = _q_usd(penalidad_usd_eq_total - use_sc)
            else:
                use_sc = D("0.00"); use_cc = D("0.00")
            buckets = Buckets(
                efectivo_usd=buckets.efectivo_usd,
                euro_usd=buckets.euro_usd,
                usdt_usd=buckets.usdt_usd,
                nequi_usd=buckets.nequi_usd,
                bs_sc_usd_eq=_q_usd(buckets.bs_sc_usd_eq - use_sc),
                bs_cc_usd_eq=_q_usd(buckets.bs_cc_usd_eq - use_cc),
            )

    # ---------- Egresos Bs directos ----------
    detalle_bs: List[Dict[str, str]] = []
    total_bs_directos = bs_bcv + bs_euro_bcv + devoluciones_bs + registro_bs
    if total_bs_directos > 0:
        bs_total = buckets.bs_sc_usd_eq + buckets.bs_cc_usd_eq
        if bs_total > 0:
            ratio_sc = buckets.bs_sc_usd_eq / bs_total
            use_sc = _q_usd(total_bs_directos * ratio_sc)
            use_cc = _q_usd(total_bs_directos - use_sc)
        else:
            use_sc = D("0.00"); use_cc = D("0.00")
        buckets = Buckets(
            efectivo_usd=buckets.efectivo_usd,
            euro_usd=buckets.euro_usd,
            usdt_usd=buckets.usdt_usd,
            nequi_usd=buckets.nequi_usd,
            bs_sc_usd_eq=_q_usd(buckets.bs_sc_usd_eq - use_sc),
            bs_cc_usd_eq=_q_usd(buckets.bs_cc_usd_eq - use_cc),
        )

        if bs_bcv:
            detalle_bs.append({"concepto": "bs_bcv", "monto_usd_eq": _format_money(bs_bcv)})
        if bs_euro_bcv:
            detalle_bs.append({"concepto": "bs_euro_bcv", "monto_usd_eq": _format_money(bs_euro_bcv)})
        if devoluciones_bs:
            detalle_bs.append({"concepto": "devoluciones_bs", "monto_usd_eq": _format_money(devoluciones_bs)})
        if registro_bs:
            detalle_bs.append({"concepto": "registro", "monto_usd_eq": _format_money(registro_bs)})

    return (
        buckets,
        detalle_directo,
        detalle_225,
        detalle_bs,
        deficit_cubierto,
        penalidad_usd_eq_total,
        penalidad_bs_total,     # NUEVO
        usd_vendidos_total      # NUEVO
    )


def _escenario(
    base_buckets: Buckets,
    egresos: Dict[str, Any],
    tasas: Dict[str, float],
    parametros: Dict[str, Any],
    colchon_pct: Decimal,
) -> EscenarioSalida:
    """Aplica egresos, penalidad, calcula colchón (restado al final) y retorna tablas/saldos."""
    colchon_map = _colchon_por_bucket(base_buckets, colchon_pct)
    colchon_total = _q_usd(sum(colchon_map.values(), D("0.00")))

    buckets_after, det_dir, det_225, det_bs, deficit_usd_bs, pen_usd_eq, pen_bs, usd_carril = _aplica_egresos_y_penalidad(
        base_buckets, egresos, tasas, parametros
    )

    saldo_divisas_final = _q_usd(
        buckets_after.efectivo_usd + buckets_after.euro_usd + buckets_after.usdt_usd + buckets_after.nequi_usd
    )
    saldo_bs_final = _q_usd(buckets_after.bs_sc_usd_eq + buckets_after.bs_cc_usd_eq)
    saldo_neto = _q_usd(saldo_divisas_final + saldo_bs_final)
    saldo_liquido = _q_usd(saldo_neto - colchon_total)

    return {
        "ingresos_por_bucket": {
            "efectivo_usd": _format_money(base_buckets.efectivo_usd),
            "euro_usd": _format_money(base_buckets.euro_usd),
            "usdt_usd": _format_money(base_buckets.usdt_usd),
            "nequi_usd": _format_money(base_buckets.nequi_usd),
            "bs_sc_usd_eq": _format_money(base_buckets.bs_sc_usd_eq),
            "bs_cc_usd_eq": _format_money(base_buckets.bs_cc_usd_eq),
            "total_usd_eq": _format_money(base_buckets.total_usd_eq),
        },
        "egresos_divisas_directo": det_dir,
        "egresos_divisas_225_247": det_225,
        "egresos_bs_directo": det_bs,
        "deficit_usd_cubierto_en_bs": _format_money(deficit_usd_bs),
        "penalidad_225_247_usd_eq": _format_money(pen_usd_eq),
        "penalidad_225_247_bs": _format_money(pen_bs),                 # NUEVO
        "usd_vendidos_por_carril": _format_money(usd_carril),          # NUEVO
        "colchon_por_bucket": {k: _format_money(v) for k, v in colchon_map.items()},
        "colchon_total": _format_money(colchon_total),
        "saldos": {
            "divisas_final_usd": _format_money(saldo_divisas_final),
            "bs_final_usd_eq": _format_money(saldo_bs_final),
            "neto_usd_eq": _format_money(saldo_neto),
            "liquido_usd_eq": _format_money(saldo_liquido),
        },
    }


def calcular_proyeccion(
    ingresos: Dict[str, Any],
    egresos: Dict[str, Any],
    tasas: Dict[str, float],
    parametros: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Calcula proyecciones mensuales en tres escenarios aplicando reglas de conversión,
    carril 225→247 (SOLO MODO MANUAL por ítem) y colchón (aplicado al final).

    Args: ver documentación previa.

    Returns:
        {
          "escenarios": { "full": ..., "base": ..., "base_mora": ... },
          "tasas_y_margenes": { "tasas": {...}, "margenes": {...} },
          "glosario": {...},
          "banderas": { "insuficiencia_bs": bool }
        }
    """
    # Validación temprana de tasas globales (se mantienen por compatibilidad/UI, pero NO se usan en modo manual)
    venta_glob = _d((tasas or {}).get("venta_usd", 0))
    recompra_glob = _d((tasas or {}).get("recompra_usd", 0))
    if venta_glob > 0 and recompra_glob > 0 and venta_glob > recompra_glob:
        raise ValueError("Inconsistencia de tasas: venta_usd no puede ser mayor que recompra_usd.")

    colchon_pct = _d(parametros.get("colchon_pct", 0))
    if colchon_pct < 0:
        raise ValueError("colchon_pct no puede ser negativo.")

    solv_imp = ingresos.get("solv_imp") or {}
    mora = ingresos.get("mora") or {}

    b_solv_imp = _build_buckets(solv_imp, tasas)
    b_mora = _build_buckets(mora, tasas) if mora else _empty_buckets()

    b_full = _sum_buckets(b_solv_imp, b_mora)
    b_base = b_solv_imp
    b_base_mora = b_full

    esc_full = _escenario(b_full, egresos, tasas, parametros, colchon_pct)
    esc_base = _escenario(b_base, egresos, tasas, parametros, colchon_pct)
    esc_base_mora = _escenario(b_base_mora, egresos, tasas, parametros, colchon_pct)

    def _bs_neg(esc: EscenarioSalida) -> bool:
        return _d(esc["saldos"]["bs_final_usd_eq"]) < 0

    banderas = {"insuficiencia_bs": any([_bs_neg(esc_full), _bs_neg(esc_base), _bs_neg(esc_base_mora)])}

    usd_bcv = _d(tasas.get("usd_bcv", 0))
    eur_bcv = _d(tasas.get("eur_bcv", 0))
    binance_actual = _d(tasas.get("binance_actual", 0))
    binance_prevision = _d(tasas.get("binance_prevision", 0))

    margenes = {
        "usd_tol": _format_money(_q_usd(usd_bcv * (D("1") + colchon_pct))),
        "eur_tol": _format_money(_q_usd(eur_bcv * (D("1") + colchon_pct))),
    }

    out = {
        "escenarios": {
            "full": esc_full,
            "base": esc_base,
            "base_mora": esc_base_mora,
        },
        "tasas_y_margenes": {
            "tasas": {
                "usd_bcv": _format_money(usd_bcv),
                "eur_bcv": _format_money(eur_bcv),
                "binance_actual": _format_money(binance_actual),
                "binance_prevision": _format_money(binance_prevision),
            },
                "margenes": margenes,
        },
        "glosario": {
            "S/C": "Sin conversión",
            "C/C": "Con conversión",
            "225→247": "Pago de USD canalizado por Bs con diferencia de tasas venta/recompra",
            "Penalidad": "Costo en Bs y su equivalente en USD-eq por Δ(recompra − venta) aplicado a USD vendidos",
            "Colchón": "Porcentaje de resguardo aplicado al total por bucket y restado al final",
            "Saldo neto": "Suma de divisas_final + bs_final",
            "Saldo líquido": "Saldo neto menos el colchón total",
        },
        "banderas": banderas,
    }
    return out
