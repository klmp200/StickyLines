from __future__ import annotations
from time import sleep
from threading import Thread
from functools import lru_cache
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass
import mdpopups
import sublime
import sublime_plugin

PHANTOM_KEY = "STICKY_LINES"
sync_manager: Optional[SyncManager] = None

def plugin_loaded():
    global sync_manager

    sync_manager = SyncManager()
    sync_manager.start()

def plugin_unloaded():
    if sync_manager:
        sync_manager.stop()

def is_plugin_enabled(view: sublime.View) -> bool:
    return bool(view.settings().get("sticky_lines_enabled", True))

def set_is_plugin_enabled(view: sublime.View, enabled: bool):
    view.settings().set("sticky_lines_enabled", enabled)

class SyncManager:
    """In charge of detecting viewport changes"""
    def __init__(self):
        self._is_running = False
        self._thread: Optional[Thread] = None
        self._last_states: Dict[sublime.View, sublime.Region] = {}

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
        if not is_plugin_enabled(view):
            return

        last_region = self._last_states.get(view, None)
        visible_region = view.visible_region()

        if last_region and visible_region == last_region:
            return

        self._last_states[view] = visible_region

        display_lines(view)

    def run(self):
        sublime.active_window().status_message("StickyLines started")
        self._is_running = True
        while self._is_running:
            view = sublime.active_window().active_view()
            if not view:
                continue

            self._handle_view(view)
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

def display_lines(view: sublime.View):
    viewport = view.visible_region()
    stack = get_symbol_stack(view, viewport)

    hide_lines(view)

    if not stack:
        return

    mdpopups.add_phantom(
        view=view,
        key=PHANTOM_KEY,
        region=viewport,
        content=create_phantom_content(view, stack),
        layout=sublime.PhantomLayout.BLOCK,
    )

class StickyLinesToggleCommand(sublime_plugin.TextCommand):
    def name(self) -> str:
        return "sticky_lines_toggle"

    def run(self, *args, **kwargs):
        set_is_plugin_enabled(self.view, not is_plugin_enabled(self.view))
        if is_plugin_enabled(self.view):
            display_lines(self.view)
            sublime.active_window().status_message("StickyLines enabled")
        else:
            hide_lines(self.view)
            sublime.active_window().status_message("StickyLines disabled")

# class ViewListener(sublime_plugin.ViewEventListener):

#     # We can't be async, this creates jittering otherwise
#     # def on_selection_modified(self):
#     #     display_lines(self.view)

#     def on_activated(self):
#         display_lines(self.view)

#     @classmethod
#     def is_applicable(self, settings: sublime.Settings) -> bool:
#         return bool(settings.get("sticky_lines_enabled", True))