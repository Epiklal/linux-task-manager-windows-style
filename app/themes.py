"""Colour schemes.

"light" and "dark" are the hand-tuned Windows 10 / Windows 11 colours. The other
schemes are popular editor/terminal palettes (Nord, Dracula, Catppuccin, ...). Each of
them only defines a handful of semantic colours; everything else - stylesheet
colours, heat map, navigation pane, chart lines - is derived from those, so every
scheme works with both the Windows 10 and the Windows 11 layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scheme:
    key: str
    name: str
    dark: bool
    window: str         # menu bar / navigation pane
    surface: str        # page background
    card: str           # inputs, buttons, menus
    text: str
    subtext: str
    border: str
    border_strong: str
    hover: str
    selection: str
    accent: str
    on_accent: str
    heat: str           # colour of busy cells in the process list
    charts: dict = field(default_factory=dict)  # cpu / memory / disk / network / gpu


# Graph colours of the Windows 10 Task Manager and brighter ones for dark backgrounds.
WIN10_CHART_COLORS = {"cpu": "#117dbb", "memory": "#8b12ae", "disk": "#4da60c",
                      "network": "#a74f01", "gpu": "#117dbb"}
WIN10_DARK_CHART_COLORS = {"cpu": "#3a9ee8", "memory": "#b35ad6", "disk": "#6cc12a",
                           "network": "#e08a2e", "gpu": "#3a9ee8"}

SCHEMES: dict[str, Scheme] = {s.key: s for s in [
    Scheme("light", "Hell (Standard)", False, "#ffffff", "#ffffff", "#ffffff", "#000000",
           "#707070", "#e5e5e5", "#7a7a7a", "#e5f3ff", "#cce8ff", "#0078d7", "#ffffff",
           "#fff4c4", WIN10_CHART_COLORS),
    Scheme("dark", "Dunkel", True, "#1c1c1c", "#1c1c1c", "#333333", "#f0f0f0",
           "#9a9a9a", "#333333", "#5a5a5a", "#1a3148", "#1f4466", "#3a96dd", "#ffffff",
           "#a0561c", WIN10_DARK_CHART_COLORS),
    Scheme("nord", "Nord", True, "#272c36", "#2e3440", "#3b4252", "#eceff4",
           "#a3abba", "#3b4252", "#4c566a", "#3b4252", "#434c5e", "#88c0d0", "#2e3440",
           "#ebcb8b", {"cpu": "#88c0d0", "memory": "#b48ead", "disk": "#a3be8c",
                       "network": "#d08770", "gpu": "#81a1c1"}),
    Scheme("dracula", "Dracula", True, "#21222c", "#282a36", "#343746", "#f8f8f2",
           "#a4a8c8", "#343746", "#44475a", "#343746", "#44475a", "#bd93f9", "#282a36",
           "#ffb86c", {"cpu": "#8be9fd", "memory": "#ff79c6", "disk": "#50fa7b",
                       "network": "#ffb86c", "gpu": "#bd93f9"}),
    Scheme("catppuccin_mocha", "Catppuccin Mocha", True, "#181825", "#1e1e2e", "#313244",
           "#cdd6f4", "#a6adc8", "#313244", "#45475a", "#313244", "#45475a", "#cba6f7",
           "#1e1e2e", "#fab387", {"cpu": "#89b4fa", "memory": "#cba6f7", "disk": "#a6e3a1",
                                  "network": "#fab387", "gpu": "#89dceb"}),
    Scheme("catppuccin_latte", "Catppuccin Latte", False, "#e6e9ef", "#eff1f5", "#ffffff",
           "#4c4f69", "#6c6f85", "#dce0e8", "#bcc0cc", "#dce0e8", "#ccd0da", "#8839ef",
           "#ffffff", "#df8e1d", {"cpu": "#1e66f5", "memory": "#8839ef", "disk": "#40a02b",
                                  "network": "#fe640b", "gpu": "#04a5e5"}),
    Scheme("gruvbox_dark", "Gruvbox Dark", True, "#1d2021", "#282828", "#3c3836",
           "#ebdbb2", "#a89984", "#3c3836", "#504945", "#3c3836", "#504945", "#fabd2f",
           "#282828", "#fe8019", {"cpu": "#83a598", "memory": "#d3869b", "disk": "#b8bb26",
                                  "network": "#fe8019", "gpu": "#8ec07c"}),
    Scheme("tokyo_night", "Tokyo Night", True, "#16161e", "#1a1b26", "#24283b", "#c0caf5",
           "#9aa5ce", "#24283b", "#3b4261", "#292e42", "#283457", "#7aa2f7", "#1a1b26",
           "#e0af68", {"cpu": "#7aa2f7", "memory": "#bb9af7", "disk": "#9ece6a",
                       "network": "#ff9e64", "gpu": "#7dcfff"}),
    Scheme("one_dark", "One Dark", True, "#21252b", "#282c34", "#2c313a", "#dcdfe4",
           "#9da5b4", "#2c313a", "#3e4451", "#2c313a", "#3e4451", "#61afef", "#282c34",
           "#d19a66", {"cpu": "#61afef", "memory": "#c678dd", "disk": "#98c379",
                       "network": "#d19a66", "gpu": "#56b6c2"}),
    Scheme("solarized_dark", "Solarized Dark", True, "#00212b", "#002b36", "#073642",
           "#eee8d5", "#93a1a1", "#073642", "#0d4654", "#073642", "#0d4654", "#268bd2",
           "#fdf6e3", "#b58900", {"cpu": "#268bd2", "memory": "#d33682", "disk": "#859900",
                                  "network": "#cb4b16", "gpu": "#2aa198"}),
    Scheme("solarized_light", "Solarized Light", False, "#eee8d5", "#fdf6e3", "#fffdf6",
           "#586e75", "#93a1a1", "#eee8d5", "#d6ceb5", "#eee8d5", "#e4dcc4", "#268bd2",
           "#fdf6e3", "#b58900", {"cpu": "#268bd2", "memory": "#d33682", "disk": "#859900",
                                  "network": "#cb4b16", "gpu": "#2aa198"}),
]}


def get_scheme(key: str) -> Scheme:
    return SCHEMES.get(key, SCHEMES["light"])


def blend(color: str, background: str, amount: float) -> str:
    """Mix `amount` (0..1) of `color` into `background`, as '#rrggbb'."""
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * amount + y * (1 - amount)):02x}" for x, y in zip(c, b))


def _is_builtin(scheme: Scheme) -> bool:
    return scheme.key in ("light", "dark")


# --- Base stylesheet palette (all layouts) --------------------------------------

_BASE_PALETTES = {
    "light": dict(
        bg="#ffffff", border="#d9d9d9", hover="#e5f3ff", status_text="#000000",
        table_bg="#ffffff", alt_row="#ffffff", sel_bg="#cce8ff", sel_fg="#000000",
        header_bg="#ffffff", header_text="#4d4d4d", input_bg="#ffffff",
        input_border="#7a7a7a", button_hover="#e5f1fb", disabled="#838383",
        tile_bg="#ffffff", tile_border="#ffffff", text="#000000", tab_text="#000000",
        tab_selected="#000000", tile_title="#000000", tile_value="#707070",
        detail_info="#000000",
    ),
    "dark": dict(
        bg="#1c1c1c", border="#333333", hover="#292929", status_text="#b8b8b8",
        table_bg="#1c1c1c", alt_row="#222222", sel_bg="#333333", sel_fg="#ffffff",
        header_bg="#242424", header_text="#e6e6e6", input_bg="#242424",
        input_border="#393939", button_hover="#313131", disabled="#777777",
        tile_bg="#1c1c1c", tile_border="#1c1c1c", text="#f1f1f1", tab_text="#c9c9c9",
        tab_selected="#ffffff", tile_title="#e6e6e6", tile_value="#b9b9b9",
        detail_info="#d0d0d0",
    ),
}


def base_palette(scheme: Scheme) -> dict:
    if _is_builtin(scheme):
        return _BASE_PALETTES[scheme.key]
    s = scheme
    return dict(
        bg=s.surface, border=s.border, hover=s.hover, status_text=s.subtext,
        table_bg=s.surface, alt_row=s.surface, sel_bg=s.selection, sel_fg=s.text,
        header_bg=s.surface, header_text=s.text, input_bg=s.card,
        input_border=s.border_strong, button_hover=s.hover, disabled=_disabled(s),
        tile_bg=s.surface, tile_border=s.surface, text=s.text, tab_text=s.subtext,
        tab_selected=s.text, tile_title=s.text, tile_value=s.subtext, detail_info=s.text,
    )


def _disabled(s: Scheme) -> str:
    return blend(s.text, s.surface, 0.45)


# --- Windows 10 layout -------------------------------------------------------------

# The Windows 10 stylesheet is written with the light Win10 colours; other schemes
# swap each of those colours for their counterpart.
_WIN10_DARK_COLORS = {
    "#ffffff": "#1c1c1c", "#000000": "#f0f0f0", "#f0f0f0": "#202020",
    "#f2f2f2": "#2b2b2b", "#e5e5e5": "#333333", "#d9d9d9": "#3a3a3a",
    "#d7d7d7": "#3f3f3f", "#cccccc": "#3a3a3a", "#bfbfbf": "#3a3a3a",
    "#cdcdcd": "#4d4d4d", "#a6a6a6": "#7a7a7a", "#adadad": "#555555",
    "#e1e1e1": "#333333", "#e5f1fb": "#2a3f55", "#cce4f7": "#1c3d5c",
    "#e5f3ff": "#1a3148", "#cce8ff": "#1f4466", "#99d1ff": "#2d6ca3",
    "#91c9f7": "#1f4f7a", "#d9ebf9": "#2a3a4a", "#0078d7": "#3a96dd",
    "#005499": "#3a96dd", "#7a7a7a": "#5a5a5a", "#838383": "#707070",
    "#707070": "#9a9a9a", "#4d4d4d": "#c8c8c8", "#4c4c4c": "#b4b4b4",
}


def win10_color_map(scheme: Scheme) -> dict[str, str]:
    """Light Win10 colour -> colour for this scheme ({} keeps the light look)."""
    if scheme.key == "light":
        return {}
    if scheme.key == "dark":
        return _WIN10_DARK_COLORS
    s = scheme
    return {
        "#ffffff": s.surface, "#000000": s.text, "#f0f0f0": s.window,
        "#f2f2f2": s.card, "#e5e5e5": s.border, "#d9d9d9": s.border,
        "#d7d7d7": s.border, "#cccccc": s.border_strong, "#bfbfbf": s.border,
        "#cdcdcd": s.border_strong, "#a6a6a6": s.subtext, "#adadad": s.border_strong,
        "#e1e1e1": s.card, "#e5f1fb": s.hover, "#cce4f7": s.selection,
        "#e5f3ff": s.hover, "#cce8ff": s.selection, "#99d1ff": s.accent,
        "#91c9f7": s.selection, "#d9ebf9": s.hover, "#0078d7": s.accent,
        "#005499": s.accent, "#7a7a7a": s.border_strong, "#838383": _disabled(s),
        "#707070": s.subtext, "#4d4d4d": s.subtext, "#4c4c4c": s.subtext,
    }


# --- Windows 11 layout -------------------------------------------------------------

_WIN11_PALETTES = {
    "light": dict(
        nav="#f3f3f3", content="#f9f9f9", card="#ffffff", text="#1a1a1a", sub="#5d5d5d",
        border="#e5e5e5", border_strong="#cccccc", hover="#f0f0f0", pressed="#f5f5f5",
        selection="#e1ecf7", accent="#005fb8", on_accent="#ffffff", scroll="#8a8a8a",
        disabled="#a0a0a0", tile_selected="#eaeaea",
    ),
    "dark": dict(
        nav="#202020", content="#272727", card="#2d2d2d", text="#ffffff", sub="#c5c5c5",
        border="#1d1d1d", border_strong="#3a3a3a", hover="#323232", pressed="#282828",
        selection="#1f3a52", accent="#60cdff", on_accent="#000000", scroll="#9a9a9a",
        disabled="#7a7a7a", tile_selected="#2d2d2d",
    ),
}


def win11_palette(scheme: Scheme) -> dict:
    if _is_builtin(scheme):
        return _WIN11_PALETTES[scheme.key]
    s = scheme
    return dict(
        nav=s.window, content=s.surface, card=s.card, text=s.text, sub=s.subtext,
        border=s.border, border_strong=s.border_strong, hover=s.hover,
        pressed=s.surface, selection=s.selection, accent=s.accent, on_accent=s.on_accent,
        scroll=s.subtext, disabled=_disabled(s), tile_selected=s.hover,
    )


# --- Colours for custom-painted widgets ------------------------------------------------

_BUILTIN_HEAT = {
    ("light", "win10"): ["#fff4c4", "#ffe8a6", "#ffd27a", "#ffa04a"],
    ("dark", "win10"): ["#2e2a1c", "#4a3f1c", "#6e521c", "#a0561c"],
    ("light", "win11"): ["#eef4fb", "#d9e8f7", "#b5d3f0", "#80b5e6"],
    ("dark", "win11"): ["#2b323b", "#23405c", "#1f5282", "#1f6fb0"],
}

_BUILTIN_WIDGET_COLORS = {
    ("light", "win10"): dict(header_bg="#ffffff", header_hover="#d9ebf9",
                             header_text="#4d4d4d", header_total="#1a1a1a",
                             header_border="#e5e5e5", chevron="#7a7a7a",
                             group="#1c3b8c", chart_bg=None),
    ("dark", "win10"): dict(header_bg="#1c1c1c", header_hover="#2a3a4a",
                            header_text="#c8c8c8", header_total="#f0f0f0",
                            header_border="#333333", chevron="#9a9a9a",
                            group="#6cb4ff", chart_bg=None),
    ("light", "win11"): dict(header_bg="#f9f9f9", header_hover="#f0f0f0",
                             header_text="#5d5d5d", header_total="#1a1a1a",
                             header_border="#e5e5e5", chevron="#9a9a9a",
                             group="#1a1a1a", chart_bg="#f9f9f9"),
    ("dark", "win11"): dict(header_bg="#272727", header_hover="#323232",
                            header_text="#c5c5c5", header_total="#ffffff",
                            header_border="#3a3a3a", chevron="#9a9a9a",
                            group="#ffffff", chart_bg="#272727"),
}

_BUILTIN_NAV = {
    "light": dict(nav_bg="#f3f3f3", nav_hover="#eaeaea", nav_selected="#eaeaea",
                  nav_text="#1a1a1a", nav_accent="#005fb8"),
    "dark": dict(nav_bg="#202020", nav_hover="#2d2d2d", nav_selected="#2d2d2d",
                 nav_text="#ffffff", nav_accent="#60cdff"),
}


def widget_colors(scheme: Scheme, style: str) -> dict:
    """Colours for widgets that paint themselves (process header, heat map,
    navigation pane, charts)."""
    if _is_builtin(scheme):
        colors = dict(_BUILTIN_WIDGET_COLORS[(scheme.key, style)])
        colors["heat"] = _BUILTIN_HEAT[(scheme.key, style)]
        colors.update(_BUILTIN_NAV[scheme.key])
    else:
        s = scheme
        colors = dict(
            header_bg=s.surface, header_hover=s.hover, header_text=s.subtext,
            header_total=s.text, header_border=s.border_strong, chevron=s.subtext,
            group=s.text if style == "win11" else s.accent, chart_bg=s.surface,
            heat=[blend(s.heat, s.surface, a) for a in (0.10, 0.25, 0.45, 0.75)],
            nav_bg=s.window, nav_hover=s.hover, nav_selected=s.selection,
            nav_text=s.text, nav_accent=s.accent,
        )
    colors["text"] = scheme.text
    colors["dark"] = scheme.dark
    colors["charts"] = scheme.charts
    return colors
