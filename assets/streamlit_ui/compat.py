"""Streamlit version compatibility.

This module exists because of a real outage. The app was deployed without a pinned
Streamlit version, so Streamlit in Snowflake defaulted to 1.22.0, and a single
`hide_index=True` on `st.dataframe` -- a kwarg added in 1.23 -- raised a TypeError
that aborted the script. The tab bar rendered, every tab was empty, and the sidebar
would not open. One unsupported keyword took the whole app down.

The version is now pinned in pyproject.toml, which is the actual fix. This layer is
the belt to that braces: the app also gets previewed locally in whatever Streamlit
the developer happens to have, and a hard AttributeError at import time is the worst
possible failure because it produces no page at all and no clue.

The rule throughout: degrade a feature, never crash. Losing cross-filtering leaves a
readable chart. Losing fragments costs a rerun. Raising TypeError costs the meeting.
"""

from __future__ import annotations

from typing import Any, Callable

import streamlit as st

# Features found missing at import. Reported once in the sidebar so a degraded
# preview is visible rather than mysterious -- and stays silent on the target
# runtime, where nothing is missing.
MISSING: list[str] = []


# Are we running inside Snowflake (a Snowsight iframe) rather than a local browser tab?
#
# Probed with st.connection, which is the supported accessor and is cached by Streamlit,
# so calling it here costs nothing beyond the connection the app already needs.
# Deliberately not get_active_session(): that is documented as not thread-safe, and on a
# container runtime every viewer shares one process.
#
# This gates URL syncing. See filters.sync_to_url for why writing query params inside the
# Snowsight iframe made every button in the app appear dead.
def _in_snowsight() -> bool:
    try:
        st.connection("snowflake")
        return True
    except Exception:
        return False


IN_SNOWSIGHT = _in_snowsight()


def _probe(name: str, *, on: Any = st) -> bool:
    return hasattr(on, name)


# --- st.fragment ----------------------------------------------------------
# Scopes a rerun to one panel. Purely a performance feature, so a no-op decorator
# is a correct fallback.
if _probe("fragment"):
    fragment = st.fragment
elif _probe("experimental_fragment"):
    fragment = st.experimental_fragment
else:
    MISSING.append("fragment (panel-scoped reruns)")

    def fragment(func: Callable | None = None, **_kw):  # type: ignore[misc]
        if func is None:
            return lambda f: f
        return func


# --- query params ---------------------------------------------------------
HAS_QUERY_PARAMS = _probe("query_params")
HAS_LEGACY_QUERY_PARAMS = _probe("experimental_get_query_params")
if not (HAS_QUERY_PARAMS or HAS_LEGACY_QUERY_PARAMS):
    MISSING.append("query params (shareable filter URLs)")


def get_query_params() -> dict[str, Any]:
    if HAS_QUERY_PARAMS:
        return dict(st.query_params)
    if HAS_LEGACY_QUERY_PARAMS:
        # The legacy API returns every value as a list.
        return {k: (v[0] if isinstance(v, list) and v else v)
                for k, v in st.experimental_get_query_params().items()}
    return {}


def set_query_params(**kw: Any) -> None:
    clean = {k: v for k, v in kw.items() if v not in (None, "", [], ())}
    if HAS_QUERY_PARAMS:
        try:
            st.query_params.clear()
            for k, v in clean.items():
                st.query_params[k] = v
        except Exception:
            # Writing query params can fail mid-rerun. A lost URL is not worth an
            # exception on screen.
            pass
    elif _probe("experimental_set_query_params"):
        try:
            st.experimental_set_query_params(**clean)
        except Exception:
            pass


# --- selection events -----------------------------------------------------
# Cross-filtering needs on_select. Probing the signature is more honest than
# checking a version number, because Snowflake's builds do not always line up
# with upstream release notes.
def _supports_kwarg(fn: Callable, kwarg: str) -> bool:
    try:
        import inspect
        return kwarg in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


SUPPORTS_CHART_SELECT = _supports_kwarg(st.plotly_chart, "on_select")
SUPPORTS_DF_SELECT = _supports_kwarg(st.dataframe, "on_select")
SUPPORTS_HIDE_INDEX = _supports_kwarg(st.dataframe, "hide_index")
SUPPORTS_COLUMN_CONFIG = _supports_kwarg(st.dataframe, "column_config")

if not SUPPORTS_CHART_SELECT:
    MISSING.append("chart click-to-filter")
if not SUPPORTS_HIDE_INDEX:
    MISSING.append("hidden dataframe index")


def plotly_chart(fig, *, key: str | None = None, on_select: str | None = None,
                 **kw: Any):
    """st.plotly_chart with selection support only where it exists."""
    kw.setdefault("use_container_width", True)
    kw.setdefault("config", {"scrollZoom": False, "displayModeBar": False})
    if on_select and SUPPORTS_CHART_SELECT:
        return st.plotly_chart(fig, key=key, on_select=on_select, **kw)
    return st.plotly_chart(fig, key=key, **kw)


def dataframe(df, *, column_config: dict | None = None,
              hide_index: bool = True, **kw: Any):
    """st.dataframe that drops kwargs the running build does not accept.

    This is the exact call that took the app down. It now cannot.
    """
    kw.setdefault("use_container_width", True)
    if column_config and SUPPORTS_COLUMN_CONFIG:
        kw["column_config"] = column_config
    if SUPPORTS_HIDE_INDEX:
        kw["hide_index"] = hide_index
    return st.dataframe(df, **kw)


# --- alerts ---------------------------------------------------------------
# Material icon shortcodes crash pre-1.31. Every alert goes through here so the
# degradation notice cannot itself be the thing that breaks.
SUPPORTS_ALERT_ICON = _supports_kwarg(st.info, "icon")


def info(msg: str, icon: str | None = None) -> None:
    st.info(msg, icon=icon) if (icon and SUPPORTS_ALERT_ICON) else st.info(msg)


def warning(msg: str, icon: str | None = None) -> None:
    st.warning(msg, icon=icon) if (icon and SUPPORTS_ALERT_ICON) else st.warning(msg)


def error(msg: str, icon: str | None = None) -> None:
    st.error(msg, icon=icon) if (icon and SUPPORTS_ALERT_ICON) else st.error(msg)


def rerun() -> None:
    (st.rerun if _probe("rerun") else st.experimental_rerun)()


def columns(n, **kw: Any):
    if "gap" in kw and not _supports_kwarg(st.columns, "gap"):
        kw.pop("gap")
    return st.columns(n, **kw)


def radio(label: str, options, **kw: Any):
    """st.radio, dropping kwargs the running build does not accept.

    `horizontal` arrived in 1.18. The navigation depends on it for layout but not for
    function, so on an older build it degrades to a vertical list rather than raising --
    which is the whole point of this module.
    """
    if "horizontal" in kw and not _supports_kwarg(st.radio, "horizontal"):
        kw.pop("horizontal")
    return st.radio(label, options, **kw)


def button(label: str, **kw: Any):
    if "use_container_width" in kw and not _supports_kwarg(st.button, "use_container_width"):
        kw.pop("use_container_width")
    return st.button(label, **kw)


def report_degradation() -> None:
    """Sidebar notice, only when something is actually unavailable."""
    if not MISSING:
        return
    with st.sidebar:
        st.caption("Running on an older Streamlit. Unavailable here: "
                   + "; ".join(MISSING))
