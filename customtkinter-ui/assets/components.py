"""Small CustomTkinter building blocks; adapt tokens and callbacks to the application."""
import customtkinter as ctk

COLORS = {"background": "#111827", "surface": "#1F2937", "border": "#374151",
          "text": "#F3F4F6", "muted": "#CBD5E1", "accent": "#2563EB",
          "accent_hover": "#1D4ED8", "success": "#166534", "warning": "#92400E"}

class Card(ctk.CTkFrame):
    def __init__(self, master, title, **kwargs):
        super().__init__(master, fg_color=COLORS["surface"], border_color=COLORS["border"],
                         border_width=1, corner_radius=8, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, text_color=COLORS["text"],
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(
                         row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.body.grid_columnconfigure(0, weight=1)

class LabeledField(ctk.CTkFrame):
    def __init__(self, master, label, variable=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=label, anchor="w", text_color=COLORS["muted"]).grid(
            row=0, column=0, sticky="ew")
        self.entry = ctk.CTkEntry(self, textvariable=variable, height=30)
        self.entry.grid(row=1, column=0, sticky="ew", pady=(2, 0))

class StatusBadge(ctk.CTkLabel):
    def __init__(self, master, text, color=COLORS["success"], **kwargs):
        super().__init__(master, text=text, fg_color=color, text_color=COLORS["text"],
                         corner_radius=6, padx=8, height=24, **kwargs)

class ActionBar(ctk.CTkFrame):
    def __init__(self, master, actions, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        for index, (label, callback) in enumerate(actions):
            ctk.CTkButton(self, text=label, command=callback, height=30,
                          fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"]).grid(
                              row=0, column=index, padx=(0 if index == 0 else 6, 0))
