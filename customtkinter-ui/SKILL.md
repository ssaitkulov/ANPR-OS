---
name: customtkinter-ui
description: Create or modernize Windows desktop interfaces built with Python and CustomTkinter, including compact layouts, dark themes, and reusable widgets.
---

# CustomTkinter UI

Build a coherent, usable Windows interface that fits the existing application and the user's requested scope. Inspect the entry point, callbacks, state, dependencies, and existing UI before changing an application. Preserve Python business logic, public interfaces, configuration, validation, persistence, and callback behavior; connect the new widgets to existing operations rather than replacing them with mock behavior. Change business logic only when requested or necessary for the UI, and explain it.

Use the project's existing CustomTkinter version and conventions. For a new UI, define shared color and typography tokens, prefer a restrained dark palette with legible contrast, clear focus and disabled states, and use compact spacing without clipping labels or controls. Respect requested branding, language, system scaling, and light mode where applicable. Arrange controls with responsive grid weights, sensible minimum sizes and scrolling for overflow; do not mix geometry managers within the same parent. Keep long operations off the Tk event loop and marshal UI updates back to the main thread. Preserve keyboard navigation and make status and errors understandable without color alone.

For reusable starting points, adapt [assets/components.py](assets/components.py): theme tokens, a compact card, labeled field, status badge, and action bar. Copy only what the task needs and wire actions and data to the real application. Do not overwrite existing project components or add dependencies unnecessarily.

Run the actual application using its normal entry point and appropriate environment when feasible. Inspect the real window at the default and a smaller practical size; exercise changed controls, keyboard focus, errors and key existing workflows without triggering unsafe external effects. Check logs and run relevant existing tests. If launch requires credentials, hardware, a display, or unavailable dependencies, use the closest safe smoke test and state exactly what could not be verified. Do not claim a visual or interaction check based only on import or compilation.
