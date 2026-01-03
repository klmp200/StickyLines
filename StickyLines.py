from __future__ import annotations
from time import sleep, monotonic
from threading import Thread
from functools import lru_cache
from typing import List, Optional, Tuple, Dict, Union
from dataclasses import dataclass, field
import mdpopups
import sublime
import sublime_plugin

PHANTOM_KEY = "STICKY_LINES"
sync_manager: Optional[SyncManager] = None
settings: sublime.Settings = None

def plugin_loaded():
    global sync_manager, settings
    settings = sublime.load_settings("StickyLines.sublime-settings")

    sync_manager = None
    if is_plugin_auto_follow_enabled():
        sync_manager = SyncManager()
        sync_manager.start()

    settings.add_on_change("enable_globally", _update_enable_globally_callback)
    settings.add_on_change("auto_follow", _update_auto_follow_callback)

def plugin_unloaded():
    if sync_manager:
        sync_manager.stop()

    settings.clear_on_change("enable_globally")
    settings.clear_on_change("auto_follow")

def _update_enable_globally_callback():
    if is_plugin_enabled_globally():
        sublime.active_window().status_message("StickyLines enabled globally")
    else:
        sublime.active_window().status_message("StickyLines disabled globally")

def _update_auto_follow_callback():
    global sync_manager
    if sync_manager:
        sync_manager.stop()
        sync_manager = None

    if is_plugin_auto_follow_enabled():
        sync_manager = SyncManager()
        sync_manager.start()

def is_plugin_auto_follow_enabled() -> bool:
    return bool(settings.get("sticky_lines_auto_follow", True))

def set_is_plugin_auto_follow_enabled(enabled: bool):
    settings.set("sticky_lines_auto_follow", enabled)
    sublime.save_settings("StickyLines.sublime-settings")

def is_plugin_enabled_globally() -> bool:
    return bool(settings.get("sticky_lines_enabled_globally", True))

def set_is_plugin_enabled_globally(enabled: bool):
    settings.set("sticky_lines_enabled_globally", enabled)
    sublime.save_settings("StickyLines.sublime-settings")

def is_plugin_enabled_on_view(view: sublime.View) -> bool:
    return bool(view.settings().get("sticky_lines_enabled_on_view", is_plugin_enabled_globally()))

def set_is_plugin_enabled_on_view(view: sublime.View, enabled: bool):
    view.settings().set("sticky_lines_enabled_on_view", enabled)

@dataclass
class Phantom:
    HISTERISIS_S = 1

    pid: int
    view: sublime.View
    _last_check: Union[float, int] = field(default_factory=monotonic)
    _last_checked_position: Optional[sublime.Region] = field(init=False)

    def __post_init__(self):
        self._last_checked_position = self.position

    @property
    def position(self) -> Optional[sublime.Region]:
        position = self.view.query_phantom(self.pid)
        if not position:
            return None

        return position[0]

    @property
    def is_on_top(self) -> bool:
        return self.position == self.view.visible_region()

    @property
    def is_stabilized(self) -> bool:
        print(f"{self._last_check + self.HISTERISIS_S} - {monotonic()}: {self._last_check + self.HISTERISIS_S < monotonic()}")
        return self._last_checked_position == self.position and self._last_check + self.HISTERISIS_S < monotonic()

    def mark_checked(self):
        current_position = self.position
        if current_position == self._last_checked_position:
            return

        self._last_checked_position = current_position
        self._last_check = monotonic()

class SyncManager:
    """In charge of detecting viewport changes"""
    def __init__(self):
        self._is_running = False
        self._thread: Optional[Thread] = None
        self._last_states: Dict[sublime.View, Phantom] = {}

    def start(self):
        for window in sublime.windows():
            for view in window.views(include_transient=True):
                self._handle_view(view)

        self._thread = Thread(target=self.run)
        self._thread.start()

    def stop(self):
        self._is_running = False
        try:
            self._thread.join(1)
        except RuntimeError:
            sublime.error_message("Could not stop StickyLines thread")
        self._thread = None

    def _handle_view(self, view: sublime.View):
        if not is_plugin_enabled_on_view(view):
            hide_lines(view)
            return

        old_phantom = self._last_states.get(view)
        if old_phantom:
            if old_phantom.is_on_top:
                return
            if not old_phantom.is_stabilized:
                old_phantom.mark_checked()
                return

        if (phantom := display_lines(view)):
            self._last_states[view] = phantom

    def run(self):
        sublime.active_window().status_message("StickyLines started")
        self._is_running = True
        while self._is_running:
            for view in sublime.active_window().views(include_transient=True):
                self._handle_view(view)
                sleep(0.3)
            sleep(0.3)

        for window in sublime.windows():
            for view in window.views():
                hide_lines(view)
        sublime.active_window().status_message("StickyLines stopped")

@dataclass(frozen=True)
class Symbol:
    view: sublime.View
    indent: int
    region: sublime.SymbolRegion

    @property
    @lru_cache
    def line(self) -> int:
        """Line of the symbol"""
        return self.view.rowcol(self.region.region.begin())[0]

    @classmethod
    def from_symbol_region(cls, view: sublime.View, symbol: sublime.SymbolRegion) -> Symbol:
        return cls(
            view=view,
            indent=view.indentation_level(symbol.region.begin()),
            region=symbol,
        )

def get_active_symbol(symbols: List[Symbol], current_line: int) -> Tuple[int, Optional[Symbol]]:
    def is_active(current_line: int, current_symbol: Symbol, next_symbol: Optional[Symbol]):
        next_line = next_symbol.line if next_symbol else len(current_symbol.view.lines(sublime.Region(0, current_symbol.view.size())))

        if current_line >= current_symbol.line and current_line < next_line:
            return True

        return False

    for i, symbol in enumerate(symbols):
        next_symbol = symbols[i+1] if i+1 < len(symbols) else None
        if is_active(current_line, symbol, next_symbol):
            return i, symbol

    return -1, None


def get_symbol_stack(view: sublime.View, viewport: sublime.Region):
    first_viewport_line = view.rowcol(viewport.begin())[0]

    symbols = [
        Symbol.from_symbol_region(view, symbol)
        for symbol in view.symbol_regions()
    ]
    active_symbol_position, active_symbol = get_active_symbol(symbols, first_viewport_line)

    def create_stack(active_symbol: Symbol, stack: List[Symbol]) -> List[Symbol]:
        if active_symbol.indent == 0:
            return []

        if not stack:
            return []

        if active_symbol.indent > stack[0].indent:
            return [stack[0]] + create_stack(stack[0], stack[1:])

        return create_stack(active_symbol, stack[1:])

    if not active_symbol:
        return []

    # We only keep symbols outside of the viewport
    if active_symbol.indent == 0:
        return [active_symbol] if not viewport.contains(active_symbol.region.region.begin()) else []

    stack = [active_symbol]
    symbols = list(reversed(symbols[:active_symbol_position]))
    stack += create_stack(active_symbol, symbols)

    # Filter out visible symbols and reorder the list
    return list(
        reversed([
            symbol for symbol in stack
            if not viewport.contains(symbol.region.region.begin())
        ])
    )

def create_phantom_content(view: sublime.View, stack: List[Symbol]) -> str:
    if not stack:
        return ""

    rendered = f"```{stack[0].region.syntax}\n"

    for symbol in stack:
        rendered += view.substr(view.full_line(symbol.region.region))

    return rendered + "\n```"

def hide_lines(view: sublime.View):
    view.erase_phantoms(PHANTOM_KEY)

def display_lines(view: sublime.View) -> Optional[Phantom]:
    viewport = view.visible_region()
    stack = get_symbol_stack(view, viewport)

    hide_lines(view)

    if not stack:
        return

    return Phantom(
        pid=mdpopups.add_phantom(
                view=view,
                key=PHANTOM_KEY,
                region=viewport,
                content=create_phantom_content(view, stack),
                layout=sublime.PhantomLayout.BLOCK,
        ),
        view=view,
    )

def display_popup(view: sublime.View):
    selection = view.sel()
    if not selection:
        return

    stack = get_symbol_stack(view, selection[0])

    return mdpopups.show_popup(
        view=view,
        content=create_phantom_content(view, stack),
        max_width=30000,
    )

class StickyLinesToggleOnViewCommand(sublime_plugin.TextCommand):
    def name(self) -> str:
        return "sticky_lines_toggle_on_view"

    def run(self, *args, **kwargs):
        set_is_plugin_enabled_on_view(self.view, not is_plugin_enabled_on_view(self.view))
        if is_plugin_enabled_on_view(self.view):
            sublime.active_window().status_message("StickyLines enabled on this view")
        else:
            sublime.active_window().status_message("StickyLines disabled on this view")

class StickyLinesShowPopupCommand(sublime_plugin.TextCommand):
    def name(self) -> str:
        return "sticky_lines_show_popup"

    def run(self, *args, **kwargs):
        display_popup(self.view)

class StickyLinesToggleGloballyCommand(sublime_plugin.ApplicationCommand):
    def name(self) -> str:
        return "sticky_lines_toggle_globally"

    def run(self):
        set_is_plugin_enabled_globally(not is_plugin_enabled_globally())

class StickyLinesToggleAutoFollowCommand(sublime_plugin.ApplicationCommand):
    def name(self) -> str:
        return "sticky_lines_toggle_auto_follow"

    def run(self):
        set_is_plugin_auto_follow_enabled(not is_plugin_auto_follow_enabled())
