from src.utils.logging_bootstrap import setup_boot_logging

log_path = setup_boot_logging()

import logging
logging.info("Entrando no main.py")

from src.email_monitor import monitorar_emails_polling

if __name__ == "__main__":
    logging.info("Iniciando monitorar_emails_polling()")
    monitorar_emails_polling()
