# src/email_monitor.py
from __future__ import annotations
import json
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
from pypdf import PdfWriter
from data.db_bill import BillRepository
from src.xml_reader import processar_cancelamento_cte, processar_cte, processar_mdfe
from src.bank_API import cancel_bill, connect_api, create_bill, get_bill_pdf


from pathlib import Path
from typing import Optional, Tuple

load_dotenv()
DOWNLOAD_FOLDER = Path(r"data\temp_XML Files")
DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
DOCUMENTS_FOLDER = Path("data") / "documentos"
MDFE_MANIFEST_FILENAME = "manifest.json"
PDF_COMPILE_EMAIL_RECIPIENT = "thiagogambati@outlook.com"
MDFE_AVERBAR_FOLDER = None

BILL_PDF_MAX_ATTEMPTS = 5
BILL_PDF_RETRY_DELAY_SECONDS = 30
API_UNAVAILABLE_STATUS_CODES = {500, 502, 503, 504}

# Configuração básica de log
logger = logging.getLogger(__name__)

repo = None

# Carregando credenciais de e-mail do ambiente
EMAIL_USER = None
EMAIL_PASS = None

def _get_email_credentials() -> tuple[str, str]:
    '''
    Carrega e cacheia credenciais apenas quando forem necessarias.
    
    Returns:
        tuple[str, str]: Credenciais de e-mail (usuário, senha).
    Raises:
        RuntimeError: Se EMAIL_USER ou EMAIL_PASS não estiverem definidos no .env.
    '''
    global EMAIL_USER, EMAIL_PASS

    if not EMAIL_USER or not EMAIL_PASS:
        load_dotenv()
        EMAIL_USER = os.getenv("EMAIL_USER")
        EMAIL_PASS = os.getenv("EMAIL_PASS")

    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER e EMAIL_PASS devem estar definidos no .env")

    return EMAIL_USER, EMAIL_PASS


def _get_repo() -> BillRepository:
    '''
    Instancia o repositorio sob demanda.
    
    Returns:
        BillRepository: Instancia do repositório de faturas.
    '''
    global repo

    if repo is None:
        repo = BillRepository()

    return repo


def _get_mdfe_averbar_folder() -> Path:
    '''
    Carrega o caminho de averbacao do MDF-e apenas quando usado.
    
    Returns:
        Path para a pasta de averbacao do MDF-e.
    Raises:
        RuntimeError: Se AVERB_PATH não estiver definido no .env.
    '''
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
    '''
    Sanitiza nomes de arquivos para evitar caracteres inválidos no Windows e limita o tamanho do nome.

    Args:
        value: Nome do arquivo a ser sanitizado.
        max_len: Tamanho máximo do nome do arquivo.
    Returns:
        Nome do arquivo sanitizado.
    '''
    value = (value or "").strip()
    value = "".join("_" if c in _INVALID_WIN else c for c in value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w .,\-()]+", "_", value)
    value = value.strip(" .")

    return value[:max_len] or "sem_nome"

def send_mail_pdf(destinatario: str, assunto: str, corpo: str, pdf_bytes: bytes, nome_arquivo: str):
    '''
    Envia um e-mail com um arquivo PDF (em bytes) como anexo.
    
    Args:
        destinatario (str): Endereço de e-mail do destinatário.
        assunto (str): Assunto do e-mail.
        corpo (str): Corpo do e-mail.
        pdf_bytes (bytes): Conteúdo do PDF a ser anexado.
        nome_arquivo (str): Nome do arquivo PDF no anexo.
    Raises:
        Exception: Se ocorrer algum erro ao enviar o e-mail.
    '''


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
    '''
    Identifica falhas que indicam indisponibilidade da API.
    
    Args:
        error: Exceção capturada durante a chamada da API.
    Returns:
        True se a falha indica indisponibilidade da API, False caso contrário.
    '''
    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True

    if isinstance(error, requests.exceptions.HTTPError):
        response = error.response
        return bool(response is not None and response.status_code in API_UNAVAILABLE_STATUS_CODES)

    return False

def _fetch_payload(imap, uid: bytes) -> Optional[bytes]:
    '''
    Busca o payload bruto do email via UID sem marcar como lido.

    Args:
        imap: Conexão IMAP autenticada.
        uid: UID da mensagem.
    Returns:
        Bytes RFC822 do email ou None se falhar/sem payload.
    '''
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
    '''
    Converte bytes do email em um objeto Message.

    Args:
        payload: Bytes do email.
        uid: UID (para log).
    Returns:
        Message ou None se falhar.
    '''
    try:
        msg = message_from_bytes(payload, policy=default)
        return msg if isinstance(msg, EmailMessage) else None
    except Exception as e:
        logger.error("[ERROR] Falha ao converter payload em email para uid %r: %s", uid, e)
        return None



def _save_attachments(msg: EmailMessage, temp_dir: Path) -> tuple[Path | None, Path | None]:
    '''
    Salva o primeiro XML e o primeiro PDF do email no diretório temporário.

    Funciona para anexos enviados como `application/octet-stream` com `name=...`, 
    e também para anexos com filename normal.

    Args:
        msg: Email parseado (EmailMessage).
        temp_dir: Diretório para salvar anexos.
    Returns:
        (xml_path, pdf_path) como Path; podem ser None.
    '''
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
    '''
    Marca a mensagem como lida (\\Seen).
    
    Args:
        imap: Conexão IMAP autenticada.
        uid: UID da mensagem a ser marcada.
    '''
    try:
        imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    except Exception as e:
        logger.warning("[WARNING] Falha ao marcar UID %r como Seen: %s", uid, e)


def _keepalive(imap) -> None:
    '''
    Executa NOOP para manter a sessão IMAP viva.
    
    Args:
        imap: Conexão IMAP autenticada.
    '''
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
    '''
    Move XML (e PDF, se existir) para o diretório definitivo.

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
    '''
    doc_dir = DOCUMENTS_FOLDER / safe_filename(chave)
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

def _save_boleto_pdf(dados: dict, pdf_bytes: bytes) -> Path:
    '''
    Salva o boleto do CT-e junto aos documentos ainda agrupados pelo CT-e.
    
    Args:
        dados (dict): Dados extraídos do CT-e.
        pdf_bytes (bytes): Conteúdo do PDF do boleto.
    
    Returns:
        Caminho do arquivo do boleto salvo.
    '''
    boleto_dir = DOCUMENTS_FOLDER / safe_filename(str(dados["chCTe"])) / "Boleto"
    boleto_dir.mkdir(parents=True, exist_ok=True)

    boleto_path = boleto_dir / f"Boleto_{safe_filename(str(dados['nCTE']))}.pdf"
    boleto_path.write_bytes(pdf_bytes)
    return boleto_path


def _merge_archive_root(source_root: Path, target_root: Path) -> None:
    '''
    Move os arquivos de uma pasta provisoria para a raiz da pasta final da carga.
    Args:
        source_root: Pasta provisoria (ex: CT-e).
        target_root: Pasta final da carga (ex: Carga MDF-e).
    '''
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


def _find_pdf_by_prefix(source_root: Path, prefix: str) -> Path | None:
    '''
    Procura o primeiro arquivo PDF na pasta com o prefixo especificado.
    
    Args:
        source_root: Pasta onde procurar.
        prefix: Prefixo do arquivo PDF a ser procurado.
    Returns:
        Caminho do arquivo PDF encontrado ou None.
    '''
    for path in sorted(source_root.rglob(f"{prefix}*.pdf")):
        if path.is_file():
            return path
    return None


def _merge_pdf_files(pdf_paths: list[Path], output_path: Path) -> Path | None:
    existing_paths = [path for path in pdf_paths if path.exists()]
    missing_paths = [path for path in pdf_paths if not path.exists()]

    for path in missing_paths:
        logger.warning("[WARNING] PDF ausente ao montar arquivo juntado: %s", path)

    if len(existing_paths) < 2:
        logger.warning("[WARNING] PDFs insuficientes para gerar arquivo juntado em %s", output_path)
        return None

    writer = PdfWriter()
    try:
        for path in existing_paths:
            writer.append(str(path))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as output_file:
            writer.write(output_file)
    finally:
        writer.close()

    logger.info("[OK] PDFs da carga juntados em %s", output_path)
    return output_path


def _manifest_path(carga_root: Path) -> Path:
    return carga_root / MDFE_MANIFEST_FILENAME


def _build_mdfe_manifest(dados: dict) -> dict:
    return {
        "chMDFe": str(dados["chMDFe"]),
        "nMDFE": str(dados["nMDFE"]),
        "uf_ini": dados.get("uf_ini"),
        "uf_fim": dados.get("uf_fim"),
        "chCTes": [str(chave_cte) for chave_cte in dados.get("chCTes") or []],
        "ctes_recebidos": [],
        "boletos_recebidos": [],
        "arquivos_cte": {},
    }


def _load_mdfe_manifest(manifest_path: Path) -> dict | None:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("[WARNING] Falha ao ler manifesto MDF-e %s: %s", manifest_path, e)
        return None


def _save_mdfe_manifest(carga_root: Path, manifest: dict) -> Path:
    manifest_path = _manifest_path(carga_root)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path


def _iter_pending_mdfe_manifests():
    if not DOCUMENTS_FOLDER.exists():
        return

    yield from DOCUMENTS_FOLDER.rglob(MDFE_MANIFEST_FILENAME)


def _append_unique(values: list, value: str) -> None:
    if value not in values:
        values.append(value)


def _mark_cte_in_manifest(
    manifest: dict,
    chave_cte: str,
    cte_recebido: bool,
    cte_pdf: Path | None,
    boleto_pdf: Path | None,
) -> None:
    chave_cte = str(chave_cte)
    arquivos_cte = manifest.setdefault("arquivos_cte", {})
    cte_entry = arquivos_cte.setdefault(chave_cte, {})

    if cte_recebido:
        _append_unique(manifest.setdefault("ctes_recebidos", []), chave_cte)

    if cte_pdf:
        cte_entry["cte_pdf"] = cte_pdf.name

    if boleto_pdf:
        _append_unique(manifest.setdefault("boletos_recebidos", []), chave_cte)
        cte_entry["boleto_pdf"] = boleto_pdf.name


def _missing_mdfe_manifest_items(manifest: dict) -> tuple[list[str], list[str]]:
    expected_ctes = [str(chave_cte) for chave_cte in manifest.get("chCTes") or []]
    received_ctes = {str(chave_cte) for chave_cte in manifest.get("ctes_recebidos") or []}
    received_boletos = {str(chave_cte) for chave_cte in manifest.get("boletos_recebidos") or []}

    missing_ctes = [chave_cte for chave_cte in expected_ctes if chave_cte not in received_ctes]
    missing_boletos = [chave_cte for chave_cte in expected_ctes if chave_cte not in received_boletos]

    return missing_ctes, missing_boletos


def _compiled_mdfe_pdf_path(carga_root: Path, manifest: dict) -> Path:
    chave_mdfe = safe_filename(str(manifest.get("chMDFe") or carga_root.name))
    return carga_root / f"MDFe{chave_mdfe}.pdf"


def _ordered_pdf_paths_for_manifest(carga_root: Path, manifest: dict) -> list[Path]:
    mdfe_pdf = _find_pdf_by_prefix(carga_root, "MDFe_")
    if not mdfe_pdf:
        logger.warning("[WARNING] PDF do MDF-e ausente para compilar carga em %s", carga_root)
        return []

    pdf_paths = [mdfe_pdf]
    arquivos_cte = manifest.get("arquivos_cte") or {}

    for chave_cte in manifest.get("chCTes") or []:
        cte_files = arquivos_cte.get(str(chave_cte)) or {}
        cte_pdf_name = cte_files.get("cte_pdf")
        boleto_pdf_name = cte_files.get("boleto_pdf")

        if cte_pdf_name:
            pdf_paths.append(carga_root / cte_pdf_name)
        if boleto_pdf_name:
            pdf_paths.append(carga_root / boleto_pdf_name)

    return pdf_paths


def _send_compiled_mdfe_pdf(manifest: dict, merged_pdf_path: Path) -> None:
    send_mail_pdf(
        PDF_COMPILE_EMAIL_RECIPIENT,
        assunto=(
            f"Documentos compilados do MDF-e {manifest.get('nMDFE')} "
            f"({manifest.get('uf_ini')} -> {manifest.get('uf_fim')})"
        ),
        corpo=(
            "Segue em anexo o arquivo compilado com MDF-e, CT-es "
            "e boletos relacionados."
        ),
        pdf_bytes=merged_pdf_path.read_bytes(),
        nome_arquivo=merged_pdf_path.name,
    )


def _try_finalize_mdfe_carga(carga_root: Path, manifest: dict) -> Path | None:
    missing_ctes, missing_boletos = _missing_mdfe_manifest_items(manifest)

    if missing_ctes or missing_boletos:
        _save_mdfe_manifest(carga_root, manifest)
        logger.info(
            "[INFO] Carga MDF-e %s pendente. CT-es faltantes: %s. Boletos faltantes: %s",
            manifest.get("nMDFE"),
            missing_ctes or "nenhum",
            missing_boletos or "nenhum",
        )
        return None

    pdf_paths = _ordered_pdf_paths_for_manifest(carga_root, manifest)
    merged_pdf_path = _merge_pdf_files(pdf_paths, _compiled_mdfe_pdf_path(carga_root, manifest))
    if not merged_pdf_path:
        _save_mdfe_manifest(carga_root, manifest)
        return None

    try:
        _send_compiled_mdfe_pdf(manifest, merged_pdf_path)
    except Exception as e:
        manifest["ultimo_erro_envio"] = str(e)
        _save_mdfe_manifest(carga_root, manifest)
        logger.exception(
            "[ERROR] PDF compilado do MDF-e %s foi gerado, mas nao enviado por email: %s",
            manifest.get("nMDFE"),
            e,
        )
        return None

    manifest_path = _manifest_path(carga_root)
    if manifest_path.exists():
        manifest_path.unlink()
        logger.info("[OK] Manifesto temporario removido apos finalizacao da carga: %s", manifest_path)

    return merged_pdf_path


def _archive_mdfe_carga(dados: dict, xml_path: Path, pdf_path: Path | None) -> tuple[Path, Path | None]:
    '''
    Cria a pasta final da carga e tenta juntar CT-es, MDF-e e boletos relacionados.
    Se ainda faltar algum CT-e/boleto, mantém um manifesto temporário para concluir depois.
    Args:
        dados: Dados extraídos do XML do MDF-e.
        xml_path: Caminho do XML do MDF-e.
        pdf_path: Caminho do PDF do MDF-e (ou None).

    Returns:
        Tupla com o caminho da pasta final da carga e o caminho do PDF juntado/enviado,
        quando a carga estiver completa.
    '''
    carga_root = DOCUMENTS_FOLDER / safe_filename(str(dados["chMDFe"]))
    carga_root.mkdir(parents=True, exist_ok=True)

    manifest = _build_mdfe_manifest(dados)

    for chave_cte in manifest.get("chCTes") or []:
        cte_root = DOCUMENTS_FOLDER / safe_filename(str(chave_cte))
        cte_recebido = cte_root.exists()
        cte_pdf = _find_pdf_by_prefix(cte_root, "CTe_")
        boleto_pdf = _find_pdf_by_prefix(cte_root, "Boleto_")
        _merge_archive_root(cte_root, carga_root)

        moved_cte_pdf = carga_root / cte_pdf.name if cte_pdf else None
        moved_boleto_pdf = carga_root / boleto_pdf.name if boleto_pdf else None
        _mark_cte_in_manifest(manifest, str(chave_cte), cte_recebido, moved_cte_pdf, moved_boleto_pdf)

    n_mdfe = safe_filename(str(dados["nMDFE"]))
    xml_filename = f"MDFe_{n_mdfe}.xml"
    mdfe_averbar_folder = _get_mdfe_averbar_folder()
    mdfe_averbar_folder.mkdir(parents=True, exist_ok=True)
    copy2(xml_path, mdfe_averbar_folder / xml_filename)
    logger.info("[OK] XML do MDF-e salvo para averbação em %s", mdfe_averbar_folder / xml_filename)
    xml_path.replace(carga_root / xml_filename)

    if pdf_path:
        mdfe_pdf_path = carga_root / f"MDFe_{n_mdfe}.pdf"
        pdf_path.replace(mdfe_pdf_path)

    return carga_root, _try_finalize_mdfe_carga(carga_root, manifest)


def _reconcile_cte_with_pending_mdfe(dados: dict) -> Path | None:
    chave_cte = str(dados["chCTe"])
    cte_root = DOCUMENTS_FOLDER / safe_filename(chave_cte)
    reconciled_pdf_path: Path | None = None
    found_manifest = False

    for manifest_path in list(_iter_pending_mdfe_manifests()):
        carga_root = manifest_path.parent
        manifest = _load_mdfe_manifest(manifest_path)
        if not manifest or chave_cte not in [str(chave) for chave in manifest.get("chCTes") or []]:
            continue

        found_manifest = True
        cte_pdf = _find_pdf_by_prefix(cte_root, "CTe_")
        boleto_pdf = _find_pdf_by_prefix(cte_root, "Boleto_")

        if cte_root.exists():
            _merge_archive_root(cte_root, carga_root)
            moved_cte_pdf = carga_root / cte_pdf.name if cte_pdf else None
            moved_boleto_pdf = carga_root / boleto_pdf.name if boleto_pdf else None
            _mark_cte_in_manifest(manifest, chave_cte, True, moved_cte_pdf, moved_boleto_pdf)
            logger.info(
                "[OK] CT-e %s vinculado a carga MDF-e %s",
                dados.get("nCTE"),
                manifest.get("nMDFE"),
            )
        else:
            logger.warning(
                "[WARNING] Manifesto MDF-e %s referencia CT-e %s, mas a pasta do CT-e nao existe: %s",
                manifest.get("nMDFE"),
                chave_cte,
                cte_root,
            )

        finalized_pdf_path = _try_finalize_mdfe_carga(carga_root, manifest)
        if finalized_pdf_path:
            reconciled_pdf_path = finalized_pdf_path

    if not found_manifest:
        logger.info("[INFO] Nenhuma carga MDF-e pendente encontrada para o CT-e %s", dados.get("nCTE"))

    return reconciled_pdf_path

def _boleto_cte(dados: dict) -> Path | None:
    '''Cria boleto do CT-e, salva no banco, baixa PDF e envia por email.

    Args:
        dados: Dados extraídos do CT-e.

    Returns:
        Caminho do PDF do boleto salvo quando o envio for concluído; None em caso de falha.
    '''
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

        boleto_path: Path | None = None
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
                    PDF_COMPILE_EMAIL_RECIPIENT,
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
            return None

        return boleto_path

    except Exception as e:
        logger.exception("[ERROR] Falha na criação do boleto: %s", e)
        return None
def _cancelar_boleto(dados):
    '''
    Cancela o boleto do CT-e, atualiza o banco e envia email de confirmação.
    
    Args:
        dados (dict): Dados extraídos do XML de cancelamento do CT-e.
    '''
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
    '''
    Processa um email contendo CT-e (XML obrigatório, PDF opcional).
    
    Args:
        imap: Conexão IMAP autenticada.
        uid: UID do email a ser processado.
    Returns:
        True se o CT-e foi processado com sucesso, False caso contrário.
    '''
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

        boleto_path = _boleto_cte(dados)
        if boleto_path:
            _reconcile_cte_with_pending_mdfe(dados)

        _keepalive(imap)
        return True

    except Exception as e:
        logger.exception("[ERROR] Erro ao processar o email UID %r: %s", uid, e)
        return False


def processar_novo_mdfe(imap, uid: bytes) -> bool:
    '''
    Processa um email contendo MDF-e (XML obrigatório, PDF opcional).
    
    Args:
        imap: Conexão IMAP autenticada.
        uid: UID do email a ser processado.
    Returns:
        True se o MDF-e foi processado com sucesso, False caso contrário.
    '''
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
        
        doc_dir, merged_pdf_path = _archive_mdfe_carga(dados, xml_path, pdf_path)
        logger.info("[OK] Carga MDF-e arquivada em %s", doc_dir)

        if merged_pdf_path:
            logger.info("[OK] PDF compilado do MDF-e enviado: %s", merged_pdf_path)
        else:
            logger.info(
                "[INFO] Carga MDF-e %s aguardando CT-es/boletos para envio do PDF compilado.",
                dados.get("nMDFE"),
            )
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
    '''
    Processa um email contendo XML de cancelamento de CT-e.
    Args:
        imap: Conexão IMAP autenticada.
        uid: UID do email a ser processado.
    Returns:
        True se o cancelamento foi processado com sucesso, False caso contrário.
    '''

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
    '''
    Polling: busca UNSEEN a cada poll_interval segundos.
    Processa backlog e novos emails.

    Args:
        poll_interval: Intervalo em segundos entre cada polling.
    '''
    
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
                                    logger.info("[OK] Email de CT-e processado com sucesso (UID %r).", uid)
                                else:
                                    logger.info(
                                        "[INFO] Email de CT-e UID %r ignorado "
                                        "(sem XML válido ou não aplicável).",
                                        uid
                                    )

                            except Exception as e:
                                logger.exception(
                                    "[ERROR] Erro não tratado ao processar CT-e UID %r: %s",
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
