# src/bank_api.py
import base64
import requests
import json
import os
import time
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from src.config import BASE_DIR

logger = logging.getLogger(__name__)



cert = BASE_DIR / "credentials" / "Inter API_Certificado.crt"
key = BASE_DIR / "credentials" / "Inter API_Chave.key"

# Variáveis de cache
_token_cache = None
_token_expiry = 0  # epoch timestamp em segundos

load_dotenv()
account_id = os.getenv("ACCOUNT_ID")

def connect_api():
    """Obtém token da API do Banco Inter (reutiliza se ainda válido por até 60 min)."""
    global _token_cache, _token_expiry

    agora = time.time()
    # se token ainda for válido, reaproveita
    if _token_cache and agora < _token_expiry:
        return _token_cache

    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")


    if not client_id or not client_secret or not cert or not key:
        raise ValueError("CLIENT_ID, CLIENT_SECRET, CERT_PATH e KEY_PATH devem estar definidos no .env")

    request_body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "boleto-cobranca.read boleto-cobranca.write pagamento-pix.write",
        "grant_type": "client_credentials",
    }

    response = requests.post(
        #'https://cdpj-sandbox.partners.uatinter.co/oauth/v2/token', 
        "https://cdpj.partners.bancointer.com.br/oauth/v2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        cert=(cert, key),
        data=request_body,
    )
    response.raise_for_status()

    token = response.json().get("access_token")
    expires_in = response.json().get("expires_in", 3600)  # padrão 1 hora

    if not token:
        raise ValueError("Não foi possível obter o access_token")

    # salva em cache
    _token_cache = token
    _token_expiry = agora + min(expires_in, 3600) - 30  # expira 30s antes por segurança

    return _token_cache


def create_bill(token: str, dados: dict, dias_vencimento: int = 14) -> dict:
    """
    Cria boleto no Banco Inter a partir dos dados do CT-e.
    dias_vencimento: número de dias até vencimento (opcional, padrão = 14).
    """

    # Validação obrigatória
    if not dados.get("cnpj_dest") or not dados.get("valor_servico"):
        raise ValueError("Dados obrigatórios ausentes: CNPJ ou valor_servico")

    cabecalhos = {
        "Authorization": f"Bearer {token}",
        "x-conta-corrente": account_id,
        "Content-Type": "application/json",
    }

    # Agrupa os números de NF-e
    nf_nums = [str(nfe.get("numero_nf", "")) for nfe in dados.get("chaves_nfe", [])]

    mensagens = {}
    linha = 1

    mensagens[f"linha{linha}"] = "Boleto referente ao transporte de produtos da(s) NF-e(s):"
    linha += 1

    for i in range(0, len(nf_nums), 5):
        chunk = nf_nums[i:i + 5]
        mensagens[f"linha{linha}"] = ", ".join(chunk)
        linha += 1

    # Vencimento dinâmico (padrão = 14 dias)
    hoje = datetime.today()
    data_vencimento = (hoje + timedelta(days=dias_vencimento)).strftime("%Y-%m-%d")

    emitir_body = {
        "seuNumero": dados["nCTE"],
        "valorNominal": float(dados["valor_servico"]),
        "valorAbatimento": 0,
        "dataVencimento": data_vencimento,
        "numDiasAgenda": 30,
        "atualizarPagador": False,
        "pagador": {
            "cpfCnpj": dados["cnpj_dest"],
            "tipoPessoa": "FISICA" if len(dados["cnpj_dest"]) == 11 else "JURIDICA",
            "nome": dados["nome_dest"],
            "endereco": dados.get("dest_logr", ""),
            "cidade": dados.get("dest_city", ""),
            "uf": dados.get("dest_UF", ""),
            "cep": dados.get("dest_cep", ""),
            "numero": dados.get("dest_num", ""),
            "complemento": dados.get("dest_comp", ""),
            "bairro": dados.get("dest_dist", ""),
            "email": dados.get("dest_email", ""),
            "ddd": dados.get("dest_ddd", ""),
            "telefone": dados.get("dest_fone", ""),
        },
        "multa": {"taxa": 2, "codigo": "PERCENTUAL"},
        "mensagem": mensagens,
    }

    response = requests.post(
        #'https://cdpj-sandbox.partners.uatinter.co/cobranca/v3/cobrancas',
        "https://cdpj.partners.bancointer.com.br/cobranca/v3/cobrancas",
        headers=cabecalhos,
        cert=(cert, key),
        data=json.dumps(emitir_body),
        timeout=30
    )

    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"Resposta inválida do Inter "
            f"({response.status_code}): {response.text}"
        )

    if not (200 <= response.status_code < 300):
        raise RuntimeError(
            f"Erro API Inter ({response.status_code}): {data}"
        )
        
    return data
def get_bill_pdf(token: str, codReq: str) -> bytes:
    """Obtém o PDF do boleto a partir do código de solicitação e retorna em bytes."""
    if not codReq:
        raise ValueError("O código de solicitação é obrigatório")

    headers = {
        "Authorization": f"Bearer {token}",
        "x-conta-corrente": account_id,
        "Content-Type": "application/json",
    }

    url_pdf = f'https://cdpj.partners.bancointer.com.br/cobranca/v3/cobrancas/{codReq}/pdf'
    response = requests.get(url_pdf, headers=headers, cert=(cert, key))
    response.raise_for_status()

    pdf_base64 = response.json().get("pdf")
    if not pdf_base64:
        raise ValueError("Campo 'pdf' ausente na resposta da API")

    return base64.b64decode(pdf_base64)


def cancel_bill(token: str, codReq: str, motivo: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "x-conta-corrente": account_id,
        "Content-Type": "application/json"
    }

    cancelar_body = {"motivoCancelamento": motivo}
    url = f"https://cdpj.partners.bancointer.com.br/cobranca/v3/cobrancas/{codReq}/cancelar"

    try:
        response = requests.post(
            url,
            headers=headers,
            cert=(cert, key),
            data=json.dumps(cancelar_body),
            timeout=30,
        )
        response.raise_for_status()
        logger.info(f"[OK] Boleto {codReq} cancelado com sucesso.")
        return response.status_code

    except requests.exceptions.HTTPError as e:
        # tenta extrair o motivo da rejeição
        try:
            error_info = response.json()
        except Exception:
            error_info = response.text

        logger.error(f"[ERROR] Falha ao cancelar boleto {codReq}: {e}\nDetalhes: {error_info}")
        raise RuntimeError(f"Falha ao cancelar boleto {codReq}: {error_info}") from e

    except Exception as e:
        logger.error(f"[ERROR] Erro inesperado ao cancelar boleto {codReq}: {e}")
        raise
