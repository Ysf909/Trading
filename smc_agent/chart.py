"""Interactive HTML chart of what the engine sees: structure, order blocks,
FVGs, liquidity, sweeps, signals and trades (requires ``plotly``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .core.engine import SMCEngine
from .core.types import LONG
from .execution.sim import Trade

COLORS = {
    "bull_ob": "rgba(41, 98, 255, 0.13)",
    "bear_ob": "rgba(255, 109, 0, 0.13)",
    "bull_fvg": "rgba(0, 200, 83, 0.16)",
    "bear_fvg": "rgba(213, 0, 0, 0.14)",
    "bull": "#089981",
    "bear": "#f23645",
    "liq": "#9c27b0",
    "tp": "rgba(8, 153, 129, 0.18)",
    "sl": "rgba(242, 54, 69, 0.18)",
}


def render_chart(
    df: pd.DataFrame,
    engine: SMCEngine,
    trades: Iterable[Trade] = (),
    path: str | Path = "chart.html",
    last_n: int = 400,
    title: str = "",
    include_plotlyjs: str | bool = "cdn",
    internal_obs: bool = False,
    blocked: Iterable[tuple[Any, list[str]]] = (),
) -> Path:
    try:
        import plotly.graph_objects as go
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit("chart output needs plotly: pip install 'smc-agent[chart]'") from exc

    n = len(df)
    start = max(0, n - last_n)
    view = df.iloc[start:]
    last = n - 1
    lo, hi = float(view["low"].min()), float(view["high"].max())
    pad = (hi - lo) * 0.08
    y_lo, y_hi = lo - pad, hi + pad

    def x(i: int) -> int:
        # integer bar positions: no weekend / session gaps on the chart
        return min(max(i, 0), last)

    def visible(a: float, b: float) -> bool:
        return max(a, b) >= y_lo and min(a, b) <= y_hi

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=list(range(start, n)), open=view["open"], high=view["high"], low=view["low"], close=view["close"],
            text=[ts.strftime("%Y-%m-%d %H:%M") for ts in view.index],
            name="price", increasing_line_color=COLORS["bull"], decreasing_line_color=COLORS["bear"],
            increasing_fillcolor=COLORS["bull"], decreasing_fillcolor=COLORS["bear"],
        )
    )

    shapes: list[dict] = []
    annotations: list[dict] = []

    # order blocks & fair value gaps
    for z in engine.zone_history:
        end = z.end if z.end >= 0 else last
        if end < start or not visible(z.top, z.bottom):
            continue
        if z.kind == "OB" and z.level != "swing" and not internal_obs:
            continue  # same default as the TradingView indicator
        key = ("bull_" if z.direction == LONG else "bear_") + z.kind.lower()
        shapes.append(dict(
            type="rect", x0=x(max(z.bar, start)), x1=x(end), y0=z.bottom, y1=z.top,
            fillcolor=COLORS[key], line=dict(width=0), layer="below",
        ))
        if z.end < 0:
            label = f"{'+' if z.direction == LONG else '-'}{z.kind}" + (f" ({z.level})" if z.level == "swing" else "")
            annotations.append(dict(x=x(end), y=z.mid, text=label, showarrow=False, xanchor="left",
                                    font=dict(size=9, color="#555")))

    # structure breaks
    for ev in engine.structure_history:
        if ev.bar < start or ev.level == "htf":
            continue
        color = COLORS["bull"] if ev.direction == LONG else COLORS["bear"]
        dash = "dot" if ev.level == "internal" else "solid"
        shapes.append(dict(type="line", x0=x(ev.pivot_bar), x1=x(ev.bar), y0=ev.price, y1=ev.price,
                           line=dict(color=color, width=1, dash=dash)))
        if ev.level == "swing" or ev.kind == "CHoCH":
            annotations.append(dict(
                x=x((ev.pivot_bar + ev.bar) // 2), y=ev.price, text=ev.kind + ("" if ev.level == "swing" else "·i"),
                showarrow=False, yshift=8 if ev.direction == LONG else -8, font=dict(size=9, color=color),
            ))

    # equal highs / lows
    for a, b, price, side in engine.eq_history:
        if b < start:
            continue
        shapes.append(dict(type="line", x0=x(a), x1=x(b), y0=price, y1=price,
                           line=dict(color=COLORS["liq"], width=1, dash="dot")))
        annotations.append(dict(x=x(b), y=price, text="EQH" if side == LONG else "EQL", showarrow=False,
                                yshift=8 if side == LONG else -8, font=dict(size=9, color=COLORS["liq"])))

    # untouched liquidity
    for lv in engine.levels:
        if not lv.major or not visible(lv.price, lv.price):
            continue
        shapes.append(dict(type="line", x0=x(max(lv.bar, start)), x1=x(last), y0=lv.price, y1=lv.price,
                           line=dict(color=COLORS["liq"], width=1, dash="dash")))
        annotations.append(dict(x=x(last), y=lv.price, text=lv.kind.upper(), showarrow=False, xanchor="left",
                                font=dict(size=9, color=COLORS["liq"])))

    # sweeps
    sweeps = [s for s in engine.sweep_history if s.bar >= start and s.major]
    if sweeps:
        fig.add_trace(go.Scatter(
            x=[x(s.bar) for s in sweeps], y=[s.price for s in sweeps], mode="markers", name="major sweep",
            marker=dict(symbol="x", size=8, color=COLORS["liq"]),
            text=[f"{s.kind} swept" for s in sweeps], hoverinfo="text+y",
        ))

    # equilibrium of the current dealing range
    eq = engine.swing.equilibrium
    if eq is not None:
        shapes.append(dict(type="line", x0=x(max(engine.swing.trail_top_bar, engine.swing.trail_bottom_bar, start)),
                           x1=x(last), y0=eq, y1=eq, line=dict(color="#888", width=1, dash="dashdot")))
        annotations.append(dict(x=x(last), y=eq, text="EQ", showarrow=False, xanchor="left",
                                font=dict(size=9, color="#888")))

    # trades
    for tr in trades:
        s = tr.signal
        if s.bar < start:
            continue
        if tr.status == "closed":
            x0, x1 = x(tr.fill_bar), x(tr.exit_bar if tr.exit_bar > tr.fill_bar else tr.fill_bar + 1)
            shapes.append(dict(type="rect", x0=x0, x1=x1, y0=s.entry, y1=s.tp, fillcolor=COLORS["tp"],
                               line=dict(width=0)))
            shapes.append(dict(type="rect", x0=x0, x1=x1, y0=s.entry, y1=s.sl, fillcolor=COLORS["sl"],
                               line=dict(width=0)))
            color = COLORS["bull"] if tr.r_multiple > 0 else COLORS["bear"]
            annotations.append(dict(x=x1, y=tr.exit_price, text=f"{tr.r_multiple:+.1f}R", showarrow=False,
                                    xanchor="left", font=dict(size=10, color=color)))
        else:
            shapes.append(dict(type="line", x0=x(s.bar), x1=x(tr.exit_bar if tr.exit_bar > 0 else s.bar + s.expiry_bars),
                               y0=s.entry, y1=s.entry, line=dict(color="#607d8b", width=1, dash="dot")))

    sigs = [sg for sg in engine.signals if sg.bar >= start]
    for d, sym, color in ((LONG, "triangle-up", COLORS["bull"]), (-1, "triangle-down", COLORS["bear"])):
        pts = [sg for sg in sigs if sg.direction == d]
        if pts:
            fig.add_trace(go.Scatter(
                x=[x(sg.bar) for sg in pts],
                y=[df["low"].iloc[sg.bar] if d == LONG else df["high"].iloc[sg.bar] for sg in pts],
                mode="markers", name="buy setup" if d == LONG else "sell setup",
                marker=dict(symbol=sym, size=11, color=color),
                text=[f"{sg.model} {sg.grade} score {sg.score}<br>entry {sg.entry:.6g} sl {sg.sl:.6g} tp {sg.tp:.6g}"
                      f" ({sg.rr:.2f}R)<br>" + "<br>".join(sg.reasons) for sg in pts],
                hoverinfo="text",
            ))

    step = max(1, (n - start) // 12)
    ticks = list(range(start, n, step))
    fig.update_xaxes(tickvals=ticks, ticktext=[df.index[i].strftime("%m-%d %H:%M") for i in ticks],
                     range=[start - 1, last + max(8, (n - start) // 12)])
    fig.update_yaxes(range=[y_lo, y_hi])
    refused = [(sg, why) for sg, why in blocked if sg.bar >= start]
    if refused:
        fig.add_trace(go.Scatter(
            x=[x(sg.bar) for sg, _ in refused],
            y=[df["low"].iloc[sg.bar] if sg.direction == LONG else df["high"].iloc[sg.bar] for sg, _ in refused],
            mode="markers", name="blocked by guard",
            marker=dict(symbol=["triangle-up-open" if sg.direction == LONG else "triangle-down-open" for sg, _ in refused],
                        size=10, color="#9e9e9e"),
            text=[f"{sg.side} {sg.model} {sg.grade} - blocked:<br>" + "<br>".join(why) for sg, why in refused],
            hoverinfo="text",
        ))

    fig.update_layout(
        title=title or f"{engine.symbol} {engine.timeframe} - SMC/ICT agent",
        shapes=shapes, annotations=annotations, template="plotly_white",
        xaxis_rangeslider_visible=False, height=820, margin=dict(l=40, r=90, t=50, b=30),
        legend=dict(orientation="h", y=1.02, x=0),
    )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(out, include_plotlyjs=include_plotlyjs, full_html=True)
    return out
