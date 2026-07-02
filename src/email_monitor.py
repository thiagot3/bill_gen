# src/email_monitor.py
from __future__ import annotations
import os
import re
from shutil import copy2, move
import time
import requests
import socket
import imaplib
import smtplib
import logging
from typing import Optional
from dotenv import load_dotenv
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from data.db_bill import BillRepository
from src.xml_reader import processar_cancelamento_cte, processar_cte, processar_mdfe
from src.bank_API import cancel_bill, connect_api, create_bill, get_bill_pdf


from pathlib import Path
from typing import Optional, Tuple

load_dotenv()
DOWNLOAD_FOLDER = Path(r"data\temp_XML Files")
DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
DOCUMENTS_FOLDER = Path("data") / "documentos"
MDFE_AVERBAR_FOLDER = None

BILL_PDF_MAX_ATTEMPTS = 3
BILL_PDF_RETRY_DELAY_SECONDS = 10
API_UNAVAILABLE_STATUS_CODES = {500, 502, 503, 504}

# Configuração básica de log
logger = logging.getLogger(__name__)

repo = None

# Carregando credenciais de e-mail do ambiente
EMAIL_USER = None
EMAIL_PASS = None

def _get_email_credentials() -> tuple[str, str]:
    """Carrega e cacheia credenciais apenas quando forem necessarias."""
    global EMAIL_USER, EMAIL_PASS

    if not EMAIL_USER or not EMAIL_PASS:
        load_dotenv()
        EMAIL_USER = os.getenv("EMAIL_USER")
        EMAIL_PASS = os.getenv("EMAIL_PASS")

    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER e EMAIL_PASS devem estar definidos no .env")

    return EMAIL_USER, EMAIL_PASS


def _get_repo() -> BillRepository:
    """Instancia o repositorio sob demanda para evitar conexao no import."""
    global repo

    if repo is None:
        repo = BillRepository()

    return repo


def _get_mdfe_averbar_folder() -> Path:
    """Carrega o caminho de averbacao do MDF-e apenas quando usado."""
    global MDFE_AVERBAR_FOLDER

    if MDFE_AVERBAR_FOLDER is None:
        load_dotenv()
        averb_path = os.getenv("AVERB_PATH")
        if not averb_path:
            raise RuntimeError("AVERB_PATH deve estar definido no .env")
        MDFE_AVERBAR_FOLDER = Path(averb_path)

    return MDFE_AVERBAR_FOLDER


def safe_filename(value: str, max_len: int = 120) -> str:
    _INVALID_WIN = '<>:"/\\|?*\0'
    value = (value or "").strip()
    value = "".join("_" if c in _INVALID_WIN else c for c in value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w .,\-()]+", "_", value)
    value = value.strip(" .")
    return value[:max_len] or "sem_nome"

def send_mail_pdf(destinatario: str, assunto: str, corpo: str, pdf_bytes: bytes, nome_arquivo: str):
    """Envia um e-mail com um arquivo PDF (em bytes) como anexo."""


    email_user, email_pass = _get_email_credentials()

    msg =EmailMessage()
    msg["From"] = email_user
    msg["To"] = destinatario
    msg["Subject"] = assunto
    msg.set_content(corpo)

    # Adiciona o PDF (sem gravar em disco)
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=nome_arquivo
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(email_user, email_pass)
            smtp.send_message(msg)
        logger.info(f"[OK] E-mail enviado para {destinatario} com o anexo {nome_arquivo}")
    except Exception as e:
        logger.error(f"[ERROR] Falha ao enviar e-mail: {e}")
        raise

def _is_api_unavailable_error(error: Exception) -> bool:
    """Identifica falhas que indicam indisponibilidade da API."""
    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True

    if isinstance(error, requests.exceptions.HTTPError):
        response = error.response
        return bool(response is not None and response.status_code in API_UNAVAILABLE_STATUS_CODES)

    return False

def _fetch_payload(imap, uid: bytes) -> Optional[bytes]:
    """Busca o payload bruto do email via UID sem marcar como lido.

    Args:
        imap: Conexão IMAP autenticada.
        uid: UID da mensagem.

    Returns:
        Bytes RFC822 do email ou None se falhar/sem payload.
    """
    status, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK" or not data:
        logger.warning("[WARNING] Falha ao fetch uid %r: %s %s", uid, status, data)
        return None

    for item in data:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])

    logger.warning("[WARNING] fetch sem payload válido para uid %r (data=%r)", uid, data)
    return None


def _parse_email(payload: bytes, uid: bytes) -> EmailMessage | None:
    """Converte bytes do email em um objeto Message.

    Args:
        payload: Bytes do email.
        uid: UID (para log).

    Returns:
        Message ou None se falhar.
    """
    try:
        msg = message_from_bytes(payload, policy=default)
        return msg if isinstance(msg, EmailMessage) else None
    except Exception as e:
        logger.error("[ERROR] Falha ao converter payload em email para uid %r: %s", uid, e)
        return None



def _save_attachments(msg: EmailMessage, temp_dir: Path) -> tuple[Path | None, Path | None]:
    """Salva o primeiro XML e o primeiro PDF do email no diretório temporário.

    Funciona para anexos enviados como `application/octet-stream` com `name=...`, 
    e também para anexos com filename normal.

    Args:
        msg: Email parseado (EmailMessage).
        temp_dir: Diretório para salvar anexos.

    Returns:
        (xml_path, pdf_path) como Path; podem ser None.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)

    xml_path: Path | None = None
    pdf_path: Path | None = None

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue

        filename = (part.get_filename() or "").strip()
        if not filename:
            filename = (part.get_param("name", header="content-type") or "").strip()
        if not filename:
            continue

        content = part.get_payload(decode=True)
        if not isinstance(content, (bytes, bytearray)):
            continue

        safe_name = Path(filename).name
        lower = safe_name.lower()
        filepath = temp_dir / safe_name

        if lower.endswith(".xml") and xml_path is None:
            filepath.write_bytes(content)
            xml_path = filepath
            logger.info("[OK] XML salvo temporariamente: %s", xml_path)

        elif lower.endswith(".pdf") and pdf_path is None:
            filepath.write_bytes(content)
            pdf_path = filepath
            logger.info("[OK] PDF salvo temporariamente: %s", pdf_path)

        if xml_path and pdf_path:
            break

    return xml_path, pdf_path

def _mark_seen(imap, uid: bytes) -> None:
    """Marca a mensagem como lida (\\Seen). Erros não interrompem o fluxo."""
    try:
        imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    except Exception as e:
        logger.warning("[WARNING] Falha ao marcar UID %r como Seen: %s", uid, e)


def _keepalive(imap) -> None:
    """Executa NOOP para manter a sessão IMAP viva."""
    try:
        imap.noop()
    except Exception:
        pass


def _archive_doc(
    kind: str,
    chave: str,
    numero: str,
    xml_path: Path,
    pdf_path: Path,
    nome: Optional[str] = None,

) -> Path:
    """Move XML (e PDF, se existir) para o diretório definitivo.

    Estrutura:
        data/documentos/<chave>/<kind>/<kind>_<numero>[_<nome>].(xml|pdf)

    Args:
        kind: "CTe" ou "MDFe".
        chave: Chave do documento.
        numero: Número do documento.
        nome: Nome opcional para compor o arquivo.
        xml_path: Caminho do XML temporário.
        pdf_path: Caminho do PDF temporário (ou None).

    Returns:
        Diretório final criado/atualizado.
    """
    doc_dir = DOCUMENTS_FOLDER / safe_filename(chave) / safe_filename(kind)
    doc_dir.mkdir(parents=True, exist_ok=True)

    n = safe_filename(str(numero))
    nm = safe_filename(str(nome)) if nome else ""

    base = f"{kind}_{n}" + (f"_{nm}" if nm else "")

    xml_dst = doc_dir / f"{base}.xml"
    xml_path.replace(xml_dst)

    if pdf_path:
        pdf_dst = doc_dir / f"{base}.pdf"
        pdf_path.replace(pdf_dst)

    return doc_dir


def _copy_doc_to_archive(
    kind: str,
    chave: str,
    numero: str,
    xml_path: Path,
    pdf_path: Path | None,
    nome: Optional[str] = None,
) -> Path:
    """Copia XML/PDF para o diretorio definitivo sem consumir o temporario."""
    doc_dir = DOCUMENTS_FOLDER / safe_filename(chave) / safe_filename(kind)
    doc_dir.mkdir(parents=True, exist_ok=True)

    n = safe_filename(str(numero))
    nm = safe_filename(str(nome)) if nome else ""
    base = f"{kind}_{n}" + (f"_{nm}" if nm else "")

    copy2(xml_path, doc_dir / f"{base}.xml")
    if pdf_path:
        copy2(pdf_path, doc_dir / f"{base}.pdf")

    return doc_dir


def _save_boleto_pdf(dados: dict, pdf_bytes: bytes) -> Path:
    """Salva o boleto do CT-e junto aos documentos ainda agrupados pelo CT-e."""
    boleto_dir = DOCUMENTS_FOLDER / safe_filename(str(dados["chCTe"])) / "Boleto"
    boleto_dir.mkdir(parents=True, exist_ok=True)

    boleto_path = boleto_dir / f"Boleto_{safe_filename(str(dados['nCTE']))}.pdf"
    boleto_path.write_bytes(pdf_bytes)
    return boleto_path


def _merge_archive_root(source_root: Path, target_root: Path) -> None:
    """
    Move os arquivos de uma pasta provisoria para a raiz da pasta final da carga.
    Args:
        source_root: Pasta provisoria (ex: CT-e).
        target_root: Pasta final da carga (ex: Carga MDF-e).
    """
    if not source_root.exists() or source_root == target_root:
        return

    target_root.mkdir(parents=True, exist_ok=True)

    for item in source_root.iterdir():
        if item.is_dir():
            for child in item.iterdir():
                if child.is_dir():
                    logger.warning("[WARNING] Subpasta inesperada ignorada ao arquivar carga: %s", child)
                    continue

                child_target = target_root / child.name
                if child_target.exists():
                    if child_target.is_dir():
                        logger.warning("[WARNING] Pasta de destino ja existe e nao sera sobrescrita: %s", child_target)
                        continue
                    child_target.unlink()
                move(str(child), str(child_target))
            item.rmdir()
            continue

        target = target_root / item.name
        if target.exists():
            if target.is_dir():
                logger.warning("[WARNING] Pasta de destino ja existe e nao sera sobrescrita: %s", target)
                continue
            target.unlink()

        move(str(item), str(target))

    try:
        source_root.rmdir()
    except OSError:
        pass


def _archive_mdfe_carga(dados: dict, xml_path: Path, pdf_path: Path | None) -> Path:
    """
    Cria a pasta final da carga e junta CT-es, MDF-e e boletos relacionados.
    Args:
        dados: Dados extraídos do XML do MDF-e.
        xml_path: Caminho do XML do MDF-e.
        pdf_path: Caminho do PDF do MDF-e (ou None).
    """
    carga_root = DOCUMENTS_FOLDER / safe_filename(str(dados["chMDFe"]))
    carga_root.mkdir(parents=True, exist_ok=True)

    for chave_cte in dados.get("chCTes") or []:
        cte_root = DOCUMENTS_FOLDER / safe_filename(str(chave_cte))
        _merge_archive_root(cte_root, carga_root)

    n_mdfe = safe_filename(str(dados["nMDFE"]))
    xml_filename = f"MDFe_{n_mdfe}.xml"
    mdfe_averbar_folder = _get_mdfe_averbar_folder()
    mdfe_averbar_folder.mkdir(parents=True, exist_ok=True)
    copy2(xml_path, mdfe_averbar_folder / xml_filename)
    logger.info("[OK] XML do MDF-e salvo para averbação em %s", mdfe_averbar_folder / xml_filename)
    xml_path.replace(carga_root / xml_filename)
    if pdf_path:
        pdf_path.replace(carga_root / f"MDFe_{n_mdfe}.pdf")

    return carga_root


def _boleto_cte(dados: dict) -> None:
    """Cria boleto do CT-e, salva no banco, baixa PDF e envia por email.

    Args:
        dados: Dados extraídos do CT-e.

    Returns:
        None
    """
    try:
        logger.info("[INFO] Conectando à API...")
        bill_repo = _get_repo()
        token = connect_api()
        logger.info("[INFO] Criando boleto...")

        response = create_bill(token, dados)
        cod_req = response.get("codigoSolicitacao")
        if not cod_req:
            raise RuntimeError(f"Resposta da API Inter sem codigoSolicitacao: {response}")
        logger.info("[OK] Boleto criado. Código da solicitação: %s", cod_req)

        bill_repo.save_table(dados, cod_req=cod_req)
        bill_repo.clean_expired()

        boleto_enviado = False

        for attempt in range(1, BILL_PDF_MAX_ATTEMPTS + 1):
            try:
                logger.info(
                    "[INFO] Baixando boleto... tentativa %s/%s",
                    attempt,
                    BILL_PDF_MAX_ATTEMPTS,
                )

                token = connect_api()
                pdf_bytes = get_bill_pdf(token, cod_req)
                logger.info("[OK] Boleto obtido")
                boleto_path = _save_boleto_pdf(dados, pdf_bytes)
                logger.info("[OK] Boleto salvo em %s", boleto_path)

                send_mail_pdf(
                    "thiagogambati@outlook.com",
                    assunto=(
                        f"Boleto referente ao CT-e {dados['nCTE']}, "
                        f"para: {dados['nome_dest']} CNPJ: {dados['cnpj_dest']}"
                    ),
                    corpo="Segue em anexo o boleto referente à prestação de serviço.",
                    pdf_bytes=pdf_bytes,
                    nome_arquivo=f"Boleto_{dados['nCTE']}.pdf",
                )

                boleto_enviado = True
                break

            except Exception as e:
                api_unavailable = _is_api_unavailable_error(e)

                if attempt < BILL_PDF_MAX_ATTEMPTS:
                    if api_unavailable:
                        logger.warning(
                            "[WARNING] API possivelmente indisponivel ao baixar boleto "
                            "na tentativa %s/%s: %s. Tentando novamente em %ss...",
                            attempt,
                            BILL_PDF_MAX_ATTEMPTS,
                            e,
                            BILL_PDF_RETRY_DELAY_SECONDS,
                        )
                    else:
                        logger.warning(
                            "[WARNING] Falha ao baixar/enviar boleto na tentativa %s/%s: %s. "
                            "Tentando novamente em %ss...",
                            attempt,
                            BILL_PDF_MAX_ATTEMPTS,
                            e,
                            BILL_PDF_RETRY_DELAY_SECONDS,
                        )
                    time.sleep(BILL_PDF_RETRY_DELAY_SECONDS)
                else:
                    if api_unavailable:
                        logger.error(
                            "[ERROR] API indisponivel ao baixar boleto apos %s tentativas: %s",
                            BILL_PDF_MAX_ATTEMPTS,
                            e,
                        )
                    else:
                        logger.error(
                            "[ERROR] Falha ao baixar/enviar boleto apos %s tentativas: %s",
                            BILL_PDF_MAX_ATTEMPTS,
                            e,
                        )

        if not boleto_enviado:
            logger.error(
                "[ERROR] Boleto do CT-e %s não foi enviado por email.",
                dados.get("nCTE"),
            )

    except Exception as e:
        logger.error("[ERROR] Falha na criação do boleto: %s", e)

def _cancelar_boleto(dados):
    bill_repo = _get_repo()

    cod_req = bill_repo.get_code_by_chcte(dados["chCTe"])

    if not cod_req:
        logger.warning(
            "[WARNING] Nenhum boleto encontrado para o CT-e %s",
            dados["chCTe"]
        )
        return

    token = connect_api()

    cancel_bill(
        token=token,
        codReq=cod_req,
        motivo=dados["motivo"]
    )

    logger.info("[OK] Boleto cancelado.")

def processar_novo_cte(imap, uid: bytes) -> bool:
    """Processa um email contendo CT-e (XML obrigatório, PDF opcional)."""
    try:
        payload = _fetch_payload(imap, uid)
        if not payload:
            return False

        msg = _parse_email(payload, uid)
        if not msg:
            return False

        xml_path, pdf_path = _save_attachments(msg, DOWNLOAD_FOLDER)

        if not xml_path:
            logger.warning("[WARNING] Email sem XML CT-e, ignorado")
            _mark_seen(imap, uid)
            return False

        dados = processar_cte(str(xml_path))
        
        if not dados:
            logger.error("[ERROR] XML inválido, descartando email")
            return False

        logger.info(
            "[INFO] Dados CT-e: n_cte=%s, destinatario=%s, cnpj_dest=%s, valor_servico=%s",
            dados["nCTE"],
            dados["nome_dest"],
            dados["cnpj_dest"],
            dados["valor_servico"],
        )

        doc_dir = _archive_doc(
            "CTe",
            chave=str(dados["chCTe"]),
            numero=str(dados["nCTE"]),
            nome=str(dados["nome_dest"]),
            xml_path=xml_path,
            pdf_path=pdf_path,
        )
        logger.info("[OK] CT-e arquivado em %s", doc_dir)

        if pdf_path:
            logger.info("[OK] PDF do CT-e arquivado")
        else:
            logger.warning("[WARNING] CT-e sem PDF")

        _mark_seen(imap, uid)

        _boleto_cte(dados)

        _keepalive(imap)
        return True

    except Exception as e:
        logger.exception("[ERROR] Erro ao processar o email UID %r: %s", uid, e)
        return False


def processar_novo_mdfe(imap, uid: bytes) -> bool:
    """Processa um email contendo MDF-e (XML obrigatório, PDF opcional)."""
    try:
        payload = _fetch_payload(imap, uid)
        if not payload:
            return False

        msg = _parse_email(payload, uid)
        if not msg:
            return False

        xml_path, pdf_path = _save_attachments(msg, DOWNLOAD_FOLDER)

        if not xml_path:
            logger.warning("[WARNING] Email sem XML MDF-e, ignorado")
            _mark_seen(imap, uid)
            return False

        dados = processar_mdfe(str(xml_path))
        if not dados:
            logger.error("[ERROR] XML inválido, descartando email")
            return False
        
        doc_dir = _archive_mdfe_carga(dados, xml_path, pdf_path)
        logger.info("[OK] Carga MDF-e arquivada em %s", doc_dir)

        if pdf_path:
            logger.info("[OK] PDF do MDF-e arquivado")
        else:
            logger.warning("[WARNING] MDF-e sem PDF")

        _mark_seen(imap, uid)
        _keepalive(imap)
        return True

    except Exception as e:
        logger.exception("[ERROR] Erro ao processar o email UID %r: %s", uid, e)
        return False
    
def processar_cancelamento(imap, uid: bytes) -> bool:
    """
    Processa um email contendo XML de cancelamento de CT-e.
    Args:
        imap: Conexão IMAP autenticada.
        uid: UID do email a ser processado.
    Returns:
        True se o cancelamento foi processado com sucesso, False caso contrário.
    """

    try:
        payload = _fetch_payload(imap, uid)
        if not payload:
            return False

        msg = _parse_email(payload, uid)
        if not msg:
            return False

        xml_path, _ = _save_attachments(msg, DOWNLOAD_FOLDER)

        if not xml_path:
            logger.warning("[WARNING] Email sem XML de cancelamento.")
            _mark_seen(imap, uid)
            return False

        dados = processar_cancelamento_cte(str(xml_path))

        if not dados:
            logger.error("[ERROR] XML de cancelamento inválido.")
            return False

        _cancelar_boleto(dados)

        try:
            os.remove(xml_path)
        except Exception:
            pass

        _mark_seen(imap, uid)
        _keepalive(imap)

        return True

    except Exception as e:
        logger.exception(
            "[ERROR] Erro ao processar cancelamento %r: %s",
            uid,
            e,
        )
        return False
    

def monitorar_emails_polling(poll_interval: int = 15):
    """
    Polling: busca UNSEEN a cada poll_interval segundos.
    Processa backlog e novos emails.
    """
    
    email_user, email_pass = _get_email_credentials()
    _get_repo()
        
    IMAP_SERVER = "imap.gmail.com"
    PORT = 993

    while True:
        mail = None
        try:
            logger.info("[INFO] Conectando ao servidor IMAP...")
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, PORT)
            mail.login(email_user, email_pass)
            mail.select("INBOX")  # caixa de entrada

            logger.info(f"[INFO] Conectado. Iniciando polling (intervalo = {poll_interval}s)...")

            while True:
                
                try:
                    mail.noop()  # força refresh da sessão

                    # --- Processa e-mails de CT-e ---
                    status_cte, data_cte = mail.uid(
                        'search', None, '(FROM "egssistemas.com.br" SUBJECT "XML CT-e" UNSEEN)'
                    )
                    if status_cte == "OK" and data_cte and data_cte[0]:
                        ids_cte = data_cte[0].split()
                        logger.info(f"[INFO] {len(ids_cte)} mensagem(ns) CT-e não lida(s) encontrada(s).")
                        for uid in ids_cte:
                            logger.info("[INFO] Processando email CT-e %r ...", uid)
                            try:
                                ok = processar_novo_cte(mail, uid)

                                if ok:
                                    logger.info("[OK] Email de cancelamento processado com sucesso (UID %r).", uid)
                                else:
                                    logger.info(
                                        "[INFO] Email de cancelamento UID %r ignorado "
                                        "(sem XML válido ou não aplicável).",
                                        uid
                                    )

                            except Exception as e:
                                logger.exception(
                                    "[ERROR] Erro não tratado ao processar cancelamento UID %r: %s",
                                    uid, e
                                )


                    # --- Processa e-mails de MDF-e ---
                    status_mdfe, data_mdfe = mail.uid(
                        'search', None, '(FROM "egssistemas.com.br" SUBJECT "XML MDF-e" UNSEEN)'
                    )
                    if status_mdfe == "OK" and data_mdfe and data_mdfe[0]:
                        ids_mdfe = data_mdfe[0].split()
                        logger.info(f"[INFO] {len(ids_mdfe)} mensagem(ns) MDF-e não lida(s) encontrada(s).")
                        for uid in ids_mdfe:
                            logger.info("[INFO] Processando email MDF-e %r ...", uid)
                            try:
                                # aqui você define a função específica para MDF-e
                                ok = processar_novo_mdfe(mail, uid)
                                if ok:
                                    logger.info("[OK] Email MDF-e processado.")
                                else:
                                    logger.error("[ERROR] Falha ao processar mensagem MDF-e %r.", uid)
                            except Exception as e:
                                logger.exception("[ERROR] Erro não tratado ao processar MDF-e %r: %s", uid, e)
                                
                    # --- Processa e-mails de Cancelamento ---
                    cancelamento_cte, data_cancelamento = mail.uid(
                        'search', None, '(FROM "egssistemas.com.br" SUBJECT "Cancelamento CT-e" UNSEEN)'
                    )
                    if cancelamento_cte == "OK" and data_cancelamento and data_cancelamento[0]:
                        ids_cancelamento = data_cancelamento[0].split()
                        logger.info(f"[INFO] {len(ids_cancelamento)} mensagem(ns) de cancelamento não lida(s) encontrada(s).")
                        for uid in ids_cancelamento:
                            logger.info("[INFO] Processando email cancelamento %r ...", uid)
                            try:
                                ok = processar_cancelamento(mail, uid)

                                if ok:
                                    logger.info("[OK] Email de cancelamento processado com sucesso (UID %r).", uid)
                                else:
                                    logger.info(
                                        "[INFO] Email de cancelamento UID %r ignorado "
                                        "(sem XML válido ou não aplicável).",
                                        uid
                                    )

                            except Exception as e:
                                logger.exception(
                                    "[ERROR] Erro não tratado ao processar cancelamento UID %r: %s",
                                    uid, e
                                )

                    time.sleep(poll_interval)
                    

                except (imaplib.IMAP4.error, socket.error) as e:
                    logger.warning(f"[WARN] Sessão IMAP caiu: {e}. Reconectando...")
                    break  # sai do loop interno para reconectar

        except Exception as e:
            logger.error(f"[ERROR] Não foi possível conectar ou autenticar: {e}. Tentando novamente em 10s...")
            time.sleep(10)

        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
            time.sleep(2)  # Espera antes de tentar reconectar
