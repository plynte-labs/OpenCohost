import customtkinter as ctk
import tkinter.messagebox as mb

from opencohost.ui.window_utils import apply_app_icon

# NOTE: This window is made modal by the caller via grab_set() after creation.
# It could adopt window_utils.show_toplevel(modal=True) in a future pass once
# the profiles flow is refactored — but do NOT change it here without a
# dedicated proposal, as grab_set ordering matters for the save callback.
class ConfiguradorPerfiles(ctk.CTkToplevel):
    def __init__(self, parent, perfiles, on_save_callback):
        super().__init__(parent)
        apply_app_icon(self)  # OpenCohost logo instead of CustomTkinter's default feather icon
        self.title("🎭 Configurador de Perfiles")
        self.geometry("800x600")
        self.configure(fg_color="#0b0f14")  # match the app's charcoal background
        self.perfiles = perfiles.copy()
        self.on_save_callback = on_save_callback
        self.perfil_actual = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.frame_lista = ctk.CTkFrame(self, width=200, fg_color="#151d26", corner_radius=14)
        self.frame_lista.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.frame_lista.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self.frame_lista, text="Perfiles Guardados", font=ctk.CTkFont(weight="bold"), text_color="#d8e2ef").grid(row=0, column=0, pady=5)
        
        self.lista_scroll = ctk.CTkScrollableFrame(self.frame_lista, fg_color="#101923", scrollbar_button_color="#2f5f8f", scrollbar_button_hover_color="#3670aa")
        self.lista_scroll.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.botones_lista = []

        self.btn_nuevo = ctk.CTkButton(self.frame_lista, text="➕ Nuevo Perfil", command=self._nuevo_perfil, fg_color="#2f5f8f", hover_color="#3670aa")
        self.btn_nuevo.grid(row=2, column=0, pady=10, padx=5)

        self.frame_editor = ctk.CTkFrame(self, fg_color="#151d26", corner_radius=14)
        self.frame_editor.grid(row=0, column=1, sticky="nsew", padx=(0, 10), pady=10)
        self.frame_editor.grid_rowconfigure(1, weight=1)
        self.frame_editor.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.frame_editor, text="Nombre del Perfil:", text_color="#d8e2ef").grid(row=0, column=0, sticky="w", padx=10, pady=10)
        self.entry_nombre = ctk.CTkEntry(self.frame_editor, fg_color="#0c1117", border_width=1, border_color="#1f2b38", corner_radius=8)
        self.entry_nombre.grid(row=0, column=1, sticky="ew", padx=10, pady=10)

        self.textbox_prompt = ctk.CTkTextbox(self.frame_editor, font=ctk.CTkFont(family="Consolas", size=12), fg_color="#090d12", border_width=1, border_color="#1f2b38")
        self.textbox_prompt.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=10, pady=5)

        self.check_system = ctk.CTkCheckBox(self.frame_editor, text="Usar rol 'system' (Desmarcar para Qwen/Gemma)", fg_color="#2f5f8f", hover_color="#3670aa", text_color="#d8e2ef")
        self.check_system.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=10)

        frame_acciones = ctk.CTkFrame(self.frame_editor, fg_color="transparent")
        frame_acciones.grid(row=3, column=0, columnspan=2, sticky="e", padx=10, pady=10)

        self.btn_eliminar = ctk.CTkButton(frame_acciones, text="🗑️ Eliminar", fg_color="darkred", hover_color="#8B0000", command=self._eliminar_perfil)
        self.btn_eliminar.pack(side="left", padx=5)

        self.btn_guardar = ctk.CTkButton(frame_acciones, text="💾 Guardar Cambios", command=self._guardar_perfil_actual, fg_color="#2f5f8f", hover_color="#3670aa")
        self.btn_guardar.pack(side="right", padx=5)

        self._actualizar_lista()
        if self.perfiles:
            self._seleccionar_perfil(list(self.perfiles.keys())[0])

    def _actualizar_lista(self):
        for btn in self.botones_lista:
            btn.destroy()
        self.botones_lista.clear()

        for nombre in self.perfiles.keys():
            btn = ctk.CTkButton(
                self.lista_scroll, text=nombre, 
                fg_color="transparent", text_color="#d8e2ef", hover_color="#1d2a38",
                anchor="w", command=lambda n=nombre: self._seleccionar_perfil(n)
            )
            btn.pack(fill="x", pady=2)
            self.botones_lista.append(btn)

    def _seleccionar_perfil(self, nombre):
        self.perfil_actual = nombre
        datos = self.perfiles[nombre]
        
        self.entry_nombre.delete(0, 'end')
        self.entry_nombre.insert(0, nombre)
        
        self.textbox_prompt.delete("1.0", "end")
        self.textbox_prompt.insert("1.0", datos["prompt"])
        
        if datos.get("use_system", False):
            self.check_system.select()
        else:
            self.check_system.deselect()

        for btn in self.botones_lista:
            if btn.cget("text") == nombre:
                btn.configure(fg_color="#1e4060")
            else:
                btn.configure(fg_color="transparent")

    def _nuevo_perfil(self):
        nuevo_nombre = "Nuevo Perfil"
        contador = 1
        while f"{nuevo_nombre} {contador}" in self.perfiles:
            contador += 1
        nuevo_nombre = f"{nuevo_nombre} {contador}"
        
        self.perfiles[nuevo_nombre] = {"prompt": "Escribe aquí la personalidad...", "use_system": False}
        self._actualizar_lista()
        self._seleccionar_perfil(nuevo_nombre)

    def _guardar_perfil_actual(self):
        if not self.perfil_actual: return
        
        nuevo_nombre = self.entry_nombre.get().strip()
        if not nuevo_nombre: return
        
        prompt = self.textbox_prompt.get("1.0", "end").strip()
        use_system = bool(self.check_system.get())

        if nuevo_nombre != self.perfil_actual:
            if nuevo_nombre in self.perfiles:
                mb.showerror("Error", "Ya existe un perfil con ese nombre", parent=self)
                return
            del self.perfiles[self.perfil_actual]
            
        self.perfiles[nuevo_nombre] = {"prompt": prompt, "use_system": use_system}
        self.perfil_actual = nuevo_nombre
        
        self._actualizar_lista()
        self._seleccionar_perfil(nuevo_nombre)
        self.on_save_callback(self.perfiles)

    def _eliminar_perfil(self):
        if not self.perfil_actual: return
        if len(self.perfiles) <= 1:
            mb.showwarning("Aviso", "No puedes eliminar el único perfil existente.", parent=self)
            return
            
        if mb.askyesno("Confirmar", f"¿Eliminar el perfil '{self.perfil_actual}'?", parent=self):
            del self.perfiles[self.perfil_actual]
            self.on_save_callback(self.perfiles)
            self._actualizar_lista()
            self._seleccionar_perfil(list(self.perfiles.keys())[0])
