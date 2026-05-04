import os
import shutil
from pathlib import Path

def mudar_modelos_a_E():
    # La ruta por defecto donde HuggingFace descargó los archivos en C:
    ruta_origen = os.path.expanduser(r"~\.cache\huggingface\hub")
    
    # La nueva casa de los modelos en E:
    carpeta_base_destino = r"E:\VoiceAI\modelos_f5"
    ruta_destino = os.path.join(carpeta_base_destino, "hub")
    
    print(f"[1/3] Buscando modelos en: {ruta_origen}")
    
    if not os.path.exists(ruta_origen):
        print("⚠️ No se encontró la carpeta original en C:. Quizás ya la moviste o la ruta es diferente.")
        return

    # Crear la carpeta contenedora en E: si no existe
    os.makedirs(carpeta_base_destino, exist_ok=True)

    if os.path.exists(ruta_destino):
        print("⚠️ Ya existe una carpeta 'hub' en el destino. Cancela si no quieres sobreescribirla.")
        return

    print(f"[2/3] 🚚 Moviendo archivos pesados a {ruta_destino}...")
    print("⏳ Por favor espera. Esto puede tardar uno o dos minutos dependiendo de la velocidad de tu disco SSD/HDD...")

    try:
        shutil.move(ruta_origen, ruta_destino)
        print("\n[3/3] ✅ ¡Éxito! Archivos movidos.")
        print("Tu disco C: ha sido liberado. Ya no se volverán a descargar ahí.")
    except Exception as e:
        print(f"\n❌ Hubo un error al mover los archivos: {e}")
        print("Asegúrate de que ningún script de Python esté corriendo (cierra otras terminales) e intenta de nuevo.")

#if __name__ == "__main__":
#    mudar_modelos_a_E()