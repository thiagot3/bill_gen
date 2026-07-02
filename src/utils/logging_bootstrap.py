import os
import sys
import logging
from datetime import datetime

def setup_boot_logging(app_name="bill_gen"):
    """
    Configura logging para arquivo, inclusive em executável PyInstaller.
    Deve ser chamado ANTES de qualquer outra coisa.
    """

    # Detecta base de execução
    if getattr(sys, 'frozen', False):
        base_dir = os.getenv("APPDATA", os.getcwd())
    else:
        base_dir = os.getcwd()

    log_dir = os.path.join(base_dir, app_name, "logs")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(
        log_dir,
        f"{app_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ]
    )

    logging.info("========================================")
    logging.info("Inicialização da aplicação")
    logging.info(f"Arquivo de log: {log_file}")
    logging.info(f"Executável: {getattr(sys, 'executable', 'python')}")
    logging.info("========================================")

    return log_file
