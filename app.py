import streamlit as st
import pandas as pd
import re
import os
import io
from datetime import timedelta

st.set_page_config(page_title="Consumo de Reactivos Mindray", layout="wide")

# =========================================================================
# CONFIGURACIÓN: nombres de reactivos y matriz reactivo-modo
# =========================================================================
DS, LH, LD, FD, LN, FN, DR, FR, VSG = (
    "DS_DILUENTE", "M6_LH_LYSE", "M6_LD_LYSE", "M6_FD_DYE",
    "M6_LN_LYSE", "M6_FN_DYE", "M6_DR_DILUENT", "M6_FR_DYE", "VSG",
)

REAGENT_DISPLAY = {
    DS: "DS DILUENTE", LH: "M-6 LH LYSE", LD: "M-6 LD LYSE", FD: "M-6 FD DYE",
    LN: "M-6 LN LYSE", FN: "M-6 FN DYE", DR: "M-6 DR DILUENT", FR: "M-6 FR DYE",
    VSG: "Reactivo solución VSG",
}

REAGENT_MODES = {
    DS:  {"CD", "CBC", "CDR", "RET", "CR", "CD+ESR", "CDR+ESR", "PLT-O"},
    LH:  {"CBC", "CD", "CDR", "CR"},
    LD:  {"CD", "CDR"},
    FD:  {"CD", "CDR"},
    LN:  {"CDR", "CD"},
    FN:  {"CDR", "CD"},
    DR:  {"CDR", "RET", "CR", "PLT-O"},
    FR:  {"CDR", "RET", "CR", "PLT-O"},
    VSG: {"CD+ESR", "CDR+ESR", "ESR"},
}

def normalize_reagent_name(raw_name):
    """Reconoce el reactivo sin importar la variante de nombre usada por el equipo."""
    if not isinstance(raw_name, str):
        return None
    name = raw_name.upper()
    if "VSG" in name:
        return VSG
    if "DS" in name and ("DILUY" in name or "DILUYE" in name):
        return DS
    if "DR" in name:
        return DR
    if "FR" in name:
        return FR
    if "FD" in name:
        return FD
    if "FN" in name:
        return FN
    if "LH" in name:
        return LH
    if "LD" in name:
        return LD
    if "LN" in name:
        return LN
    return None

def reagent_used_by_control(reagent, lot):
    """MB -> todos menos DR/FR.  ME -> DR/FR y también DS DILUENTE.  Otro prefijo -> None (revisar)."""
    lot = (lot or "").upper()
    if lot.startswith("MB"):
        return reagent not in (DR, FR)
    if lot.startswith("ME"):
        return reagent in (DR, FR, DS)
    return None


# =========================================================================
# LECTURA Y PARSEO (acepta UploadedFile de Streamlit)
# =========================================================================

def read_mindray_csv(file_obj):
    file_obj.seek(0)
    return pd.read_csv(file_obj, encoding="utf-16-le", sep="\t")


DETAIL_RE = re.compile(
    r"^\s*([^:]+?):.*?Volum\.\:\s*([\d.]+)\s*->\s*([\d.]+)", re.IGNORECASE
)

def parse_log_reactivos(file_obj, dedup_minutes=60):
    df = read_mindray_csv(file_obj)
    df = df[df["Resumen"].astype(str).str.strip() == "Modif conf reactivos"].copy()
    df["Fech/Hora"] = pd.to_datetime(df["Fech/Hora"], dayfirst=True, errors="coerce")

    parsed = df["Detalle"].astype(str).apply(lambda d: DETAIL_RE.match(d.strip()))
    df["reactivo_raw"] = parsed.apply(lambda m: m.group(1).strip() if m else None)
    df["vol_ant"] = parsed.apply(lambda m: float(m.group(2)) if m else None)
    df["vol_nuevo"] = parsed.apply(lambda m: float(m.group(3)) if m else None)
    df["reactivo"] = df["reactivo_raw"].apply(normalize_reagent_name)

    df = df.dropna(subset=["reactivo", "Fech/Hora"]).copy()
    df = df.sort_values(["reactivo", "Fech/Hora"]).reset_index(drop=True)

    keep_rows, last_kept_time = [], {}
    for _, row in df.iterrows():
        r, t = row["reactivo"], row["Fech/Hora"]
        if r in last_kept_time and (t - last_kept_time[r]) <= timedelta(minutes=dedup_minutes):
            continue
        keep_rows.append(row)
        last_kept_time[r] = t

    events = pd.DataFrame(keep_rows).reset_index(drop=True)
    events = events[["reactivo", "Fech/Hora", "vol_ant", "vol_nuevo", "reactivo_raw"]]
    events = events.rename(columns={"Fech/Hora": "fecha_hora"})
    return events.sort_values(["reactivo", "fecha_hora"]).reset_index(drop=True)


def parse_review(file_obj):
    df = read_mindray_csv(file_obj)
    df = df[["ID muestr", "Modo recue", "Panel prue", "Fec.", "Hora"]].copy()
    df["fecha_hora"] = pd.to_datetime(
        df["Fec."].astype(str).str.strip() + " " + df["Hora"].astype(str).str.strip(),
        dayfirst=True, errors="coerce",
    )
    df["Panel prue"] = df["Panel prue"].astype(str).str.strip().str.upper()
    return df.dropna(subset=["fecha_hora"]).reset_index(drop=True)


def parse_ljqc(file_obj):
    file_obj.seek(0)
    raw_lines = io.TextIOWrapper(file_obj, encoding="utf-16-le").readlines()

    def cell(line, idx):
        parts = line.rstrip("\n").split("\t")
        return parts[idx].strip() if idx < len(parts) else ""

    lote = cell(raw_lines[0], 4)
    nivel = cell(raw_lines[0], 7)
    modo = cell(raw_lines[1], 4)
    panel = cell(raw_lines[2], 1).strip().upper()

    rows = []
    for line in raw_lines[4:]:
        parts = line.rstrip("\n").split("\t")
        if not parts or parts[0].strip() in ("Destin", "Límit", ""):
            continue
        fecha = parts[1].strip() if len(parts) > 1 else ""
        hora = parts[2].strip() if len(parts) > 2 else ""
        if not fecha or not hora:
            continue
        rows.append({"fecha": fecha, "hora": hora})

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["fecha_hora"] = pd.to_datetime(out["fecha"] + " " + out["hora"], dayfirst=True, errors="coerce")
    out = out.dropna(subset=["fecha_hora"])
    out["lote"] = lote
    out["nivel"] = nivel
    out["modo"] = modo
    out["panel"] = panel if panel else "CD"
    return out[["fecha_hora", "lote", "nivel", "modo", "panel"]].reset_index(drop=True)


def parse_all_controls(file_objs):
    frames = [parse_ljqc(f) for f in file_objs]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["fecha_hora", "lote", "nivel", "modo", "panel"])
    return pd.concat(frames, ignore_index=True)


# =========================================================================
# CÁLCULO DE CONSUMO
# =========================================================================

def compute_consumption(events, samples, controls):
    detail_rows = []
    for reactivo, ev in events.groupby("reactivo"):
        ev = ev.sort_values("fecha_hora").reset_index(drop=True)
        modes_for_reagent = REAGENT_MODES.get(reactivo, set())

        for i in range(len(ev)):
            start = ev.loc[i, "fecha_hora"]
            vol_nuevo_inicio = ev.loc[i, "vol_nuevo"]
            is_last = i == len(ev) - 1
            end = ev.loc[i + 1, "fecha_hora"] if not is_last else None
            vol_ant_fin = ev.loc[i + 1, "vol_ant"] if not is_last else None

            if end is not None:
                mask_s = (samples["fecha_hora"] >= start) & (samples["fecha_hora"] < end)
            else:
                mask_s = samples["fecha_hora"] >= start
            n_samples = samples.loc[mask_s & samples["Panel prue"].isin(modes_for_reagent)].shape[0]

            if end is not None:
                mask_c = (controls["fecha_hora"] >= start) & (controls["fecha_hora"] < end)
            else:
                mask_c = controls["fecha_hora"] >= start
            sub_c = controls.loc[mask_c].copy()
            if not sub_c.empty:
                sub_c["usa_reactivo"] = sub_c["lote"].apply(lambda lot: reagent_used_by_control(reactivo, lot))
                n_controls = int(sub_c["usa_reactivo"].fillna(False).sum())
                n_no_reconocido = int(sub_c["usa_reactivo"].isna().sum())
            else:
                n_controls, n_no_reconocido = 0, 0

            n_total = n_samples + n_controls
            vol_consumido = (vol_nuevo_inicio - vol_ant_fin) if not is_last else None
            vol_por_prueba = (vol_consumido / n_total) if (vol_consumido is not None and n_total > 0) else None

            detail_rows.append({
                "reactivo": REAGENT_DISPLAY.get(reactivo, reactivo),
                "inicio_periodo": start,
                "fin_periodo": end if end is not None else "EN USO (frasco actual)",
                "vol_frasco_nuevo": vol_nuevo_inicio,
                "vol_remanente_al_cambiar": vol_ant_fin,
                "vol_consumido": vol_consumido,
                "n_muestras": n_samples,
                "n_controles": n_controls,
                "n_controles_lote_no_reconocido": n_no_reconocido,
                "n_pruebas_total": n_total,
                "vol_por_prueba": vol_por_prueba,
            })
    return pd.DataFrame(detail_rows)


def summarize_by_reagent(detail):
    """Rendimiento por caja/botella: SOLO cantidad de pruebas, sin ningún volumen."""
    cerradas = detail[detail["fin_periodo"] != "EN USO (frasco actual)"].copy()
    if cerradas.empty:
        return pd.DataFrame(columns=["reactivo", "cajas_completas", "promedio_pruebas_x_caja",
                                      "minimo", "maximo"])
    summary = cerradas.groupby("reactivo")["n_pruebas_total"].agg(
        cajas_completas="count",
        promedio_pruebas_x_caja="mean",
        minimo="min",
        maximo="max",
    ).round(1).reset_index()
    return summary.sort_values("reactivo").reset_index(drop=True)


def strip_volumes(detail):
    """Vista de detalle sin ninguna columna de volumen, solo conteo de pruebas."""
    return detail[[
        "reactivo", "inicio_periodo", "fin_periodo", "n_muestras", "n_controles",
        "n_controles_lote_no_reconocido", "n_pruebas_total",
    ]].copy()


def to_excel_bytes(summary, detail, samples, controls, events):
    buf = io.BytesIO()
    muestras_por_panel = samples["Panel prue"].value_counts().rename_axis("Panel").reset_index(name="n_muestras")

    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Rendimiento_x_Caja", index=False)
        muestras_por_panel.to_excel(xw, sheet_name="Muestras_por_Panel", index=False)
        controls.to_excel(xw, sheet_name="Controles", index=False)
        events[["reactivo", "fecha_hora"]].rename(
            columns={"fecha_hora": "fecha_cambio"}
        ).to_excel(xw, sheet_name="Fechas_de_Cambio", index=False)

        # Una hoja POR SEPARADO para cada reactivo, solo con cantidad de pruebas
        for reactivo in detail["reactivo"].unique():
            sub = detail[detail["reactivo"] == reactivo].copy().reset_index(drop=True)
            sub.insert(0, "Nº caja/cambio", range(1, len(sub) + 1))
            sub = sub.rename(columns={
                "inicio_periodo": "Fecha inicio caja",
                "fin_periodo": "Fecha fin caja (o en uso)",
                "n_muestras": "Muestras",
                "n_controles": "Controles",
                "n_pruebas_total": "TOTAL pruebas",
            })[["Nº caja/cambio", "Fecha inicio caja", "Fecha fin caja (o en uso)",
                "Muestras", "Controles", "TOTAL pruebas"]]
            hoja = reactivo.replace(" ", "_")[:31]
            sub.to_excel(xw, sheet_name=hoja, index=False)

    buf.seek(0)
    return buf


# =========================================================================
# INTERFAZ STREAMLIT
# =========================================================================

st.title("🧪 Consumo de Reactivos — Analizadores Hematológicos Mindray")
st.caption("BC-6000 · BC-6200 · BC-6800 PLUS · BC-760 · BC-780")

with st.expander("ℹ️ Cómo funciona", expanded=False):
    st.markdown("""
    1. Sube el **Log** de cambios de reactivo, el **Review** de resultados, y opcionalmente
       hasta 6 archivos **LJQC** de control (bajo/medio/alto).
    2. Cada cambio de reactivo en el Log define el inicio de un frasco/caja nueva. Entre dos
       cambios consecutivos se cuenta cuántas muestras/controles se hicieron con esa caja, para
       obtener el **rendimiento (cantidad de pruebas) por caja**.
    3. Reglas de control: lote **MB** = todos los reactivos excepto DR DILUENT/FR DYE;
       lote **ME** = DR DILUENT/FR DYE y también DS DILUENTE.
    4. Eventos de cambio duplicados a menos de 60 minutos (reconfirmación de código de barras)
       se deduplican automáticamente, conservando el primero.
    """)

col1, col2 = st.columns(2)
with col1:
    log_file = st.file_uploader("Log de cambio de reactivos (Log_....csv)", type="csv")
with col2:
    review_file = st.file_uploader("Resultados de muestras (Review_....csv)", type="csv")

ljqc_files = st.file_uploader(
    "Archivos de control LJQC (opcional, hasta 6: bajo/medio/alto)",
    type="csv", accept_multiple_files=True,
)

dedup_minutes = st.sidebar.slider("Umbral de deduplicación de cambios (minutos)", 5, 180, 60, 5)

if log_file and review_file:
    with st.spinner("Procesando..."):
        events = parse_log_reactivos(log_file, dedup_minutes=dedup_minutes)
        samples = parse_review(review_file)
        controls = parse_all_controls(ljqc_files) if ljqc_files else pd.DataFrame(
            columns=["fecha_hora", "lote", "nivel", "modo", "panel"]
        )
        detail = compute_consumption(events, samples, controls)
        summary = summarize_by_reagent(detail)

    m1, m2, m3 = st.columns(3)
    m1.metric("Eventos de cambio (deduplicados)", len(events))
    m2.metric("Muestras cargadas", len(samples))
    m3.metric("Corridas de control cargadas", len(controls))

    # --- avisos de calidad de datos ---
    lotes_control = controls["lote"].dropna().unique() if not controls.empty else []
    lotes_no_reconocidos = [l for l in lotes_control if not (str(l).upper().startswith("MB") or str(l).upper().startswith("ME"))]
    if len(lotes_no_reconocidos):
        st.warning(f"Lotes de control con prefijo no reconocido (ni MB ni ME): {lotes_no_reconocidos}")

    if not events.empty:
        n_muestras_antes = samples[samples["fecha_hora"] < events["fecha_hora"].min()].shape[0]
        if n_muestras_antes:
            st.info(
                f"{n_muestras_antes} muestras son anteriores al cambio de reactivo más antiguo "
                f"registrado en el Log y no entran en ningún periodo de consumo."
            )

    st.subheader("📦 Rendimiento por caja/botella (cantidad de pruebas)")
    st.caption("Calculado solo sobre cajas ya terminadas (se excluye la que sigue en uso).")
    st.dataframe(summary, use_container_width=True)
    if not summary.empty:
        st.bar_chart(summary.set_index("reactivo")["promedio_pruebas_x_caja"])

    abiertas = detail[detail["fin_periodo"] == "EN USO (frasco actual)"][["reactivo", "n_pruebas_total"]]
    abiertas = abiertas.rename(columns={"n_pruebas_total": "pruebas_caja_actual"})
    comparacion = abiertas.merge(summary[["reactivo", "promedio_pruebas_x_caja"]], on="reactivo", how="left")
    if not comparacion.empty:
        comparacion["%_del_promedio"] = (
            comparacion["pruebas_caja_actual"] / comparacion["promedio_pruebas_x_caja"] * 100
        ).round(0)
        st.markdown("**Caja actual en uso vs. promedio histórico:**")
        st.dataframe(comparacion, use_container_width=True)

    with st.expander("Cantidad de pruebas por caja/cambio (por reactivo)"):
        reactivo_sel = st.selectbox("Reactivo", sorted(detail["reactivo"].unique()))
        sub = detail[detail["reactivo"] == reactivo_sel].copy().reset_index(drop=True)
        sub.insert(0, "Nº caja/cambio", range(1, len(sub) + 1))
        sub = sub.rename(columns={
            "inicio_periodo": "Fecha inicio caja",
            "fin_periodo": "Fecha fin caja (o en uso)",
            "n_muestras": "Muestras",
            "n_controles": "Controles",
            "n_pruebas_total": "TOTAL pruebas",
        })[["Nº caja/cambio", "Fecha inicio caja", "Fecha fin caja (o en uso)", "Muestras", "Controles", "TOTAL pruebas"]]
        st.dataframe(sub, use_container_width=True)

    excel_bytes = to_excel_bytes(summary, detail, samples, controls, events)
    st.download_button(
        "📥 Descargar reporte Excel completo",
        data=excel_bytes,
        file_name="Consumo_Reactivos.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
else:
    st.info("Sube al menos el archivo Log y el archivo Review para comenzar.")
