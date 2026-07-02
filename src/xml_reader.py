#src/xml_reader.py
from __future__ import annotations
import os
import xml.etree.ElementTree as ET
import logging
from pathlib import Path
from typing import Any, Optional
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)
# Diretório onde os arquivos XML são armazenados
XML_DIR = "XML Files"

# Namespaces do CT-e
NS = {
    'cte': 'http://www.portalfiscal.inf.br/cte'
}

def processar_cte(xml_path):
    """Lê um arquivo XML de CT-e, extrai informações estruturadas e remove o arquivo após leitura."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Número do CTe
        ide = root.find('.//cte:ide', NS)
        nCTE = ide.find('cte:nCT', NS).text if ide is not None else None

        #Chave do CTe
        chCTe = root.find('.//cte:chCTe', NS)
        chCTe = chCTe.text if chCTe is not None else None
        
        # Remetente
        rem = root.find('.//cte:rem', NS)
        nome_rem = rem.find('cte:xNome', NS).text if rem is not None else None

        # Destinatário
        dest = root.find('.//cte:dest', NS)
        addr = dest.find('.//cte:enderDest', NS) if dest is not None else None
        dest_cnpj = dest.find('cte:CNPJ', NS).text if dest is not None and dest.find('cte:CNPJ', NS) is not None else None
        dest_nome = dest.find('cte:xNome', NS).text if dest is not None and dest.find('cte:xNome', NS) is not None else None
        dest_logr = addr.find('cte:xLgr', NS).text if addr is not None and addr.find('cte:xLgr', NS) is not None else None
        dest_num = addr.find('cte:nro', NS).text if addr is not None and addr.find('cte:nro', NS) is not None else None
        dest_dist = addr.find('cte:xBairro', NS).text if addr is not None and addr.find('cte:xBairro', NS) is not None else None
        dest_city = addr.find('cte:xMun', NS).text if addr is not None and addr.find('cte:xMun', NS) is not None else None
        dest_cep = addr.find('cte:CEP', NS).text if addr is not None and addr.find('cte:CEP', NS) is not None else None
        dest_UF = addr.find('cte:UF', NS).text if addr is not None and addr.find('cte:UF', NS) is not None else None
        dest_comp = addr.find('cte:xCpl', NS).text if addr is not None and addr.find('cte:xCpl', NS) is not None else None

        # Valores da prestação
        vprest = root.find('.//cte:vPrest', NS)
        valor_servico = vprest.find('cte:vTPrest', NS).text if vprest is not None else None

        # Carga
        infCarga = root.find('.//cte:infCarga', NS)
        v_carga = infCarga.find('cte:vCarga', NS).text if infCarga is not None else None
        produto_pred = infCarga.find('cte:proPred', NS).text if infCarga is not None else None

        # NF-es relacionadas
        chaves_nfe = []
        for infNFe in root.findall('.//cte:infNFe', NS):
            chave = infNFe.find('cte:chave', NS).text
            if chave and len(chave) == 44:
                try:
                    numero_nf = int(chave[25:34])
                except ValueError:
                    numero_nf = None
                chaves_nfe.append({"chave": chave, "numero_nf": numero_nf})

        # Validação obrigatória
        if not nCTE or not dest_cnpj or not dest_nome:
            raise ValueError(
                f"CT-e inválido: campos obrigatórios ausentes "
                f"(nCTE={nCTE}, cnpj_dest={dest_cnpj}, nome_dest={dest_nome})"
            )

        dados = {
            "nCTE": nCTE,
            "chCTe": chCTe,
            "nome_rem": nome_rem,
            "cnpj_dest": dest_cnpj,
            "nome_dest": dest_nome,
            "dest_logr": dest_logr,
            "dest_num": dest_num,
            "dest_dist": dest_dist,
            "dest_city": dest_city,
            "dest_cep": dest_cep,
            "dest_UF": dest_UF,
            "dest_comp": dest_comp,
            "valor_servico": valor_servico,
            "v_carga": v_carga,
            "produto_pred": produto_pred,
            "chaves_nfe": chaves_nfe
        }

        logger.info(f" Arquivo {os.path.basename(xml_path)} processado.")
        return dados

    except ET.ParseError:
        logger.error(f"❌ Erro ao processar XML (arquivo inválido): {xml_path}")
        return None
    except ValueError as e:
        logger.error(f"❌ Validação falhou: {e}. Arquivo {os.path.basename(xml_path)} não será usado.")
        return None
    except Exception as e:
        logger.error(f"❌ Erro inesperado ao processar {xml_path}: {e}")
        return None

def processar_mdfe(xml_path: str | Path) -> dict[str, Any] | None:
    """Extrai dados essenciais de um XML MDF-e (mdfeProc/MDFe).

    Campos retornados (principais para arquivamento):
        - chMDFe: chave do MDF-e (44 dígitos, preferindo infProt/chMDFe e caindo para infMDFe@Id)
        - nMDFE: número do MDF-e (ide/nMDF)
        - nome_emit: emit/xNome
        - cnpj_emit: emit/CNPJ ou emit/CPF
        - uf_ini: ide/UFIni
        - uf_fim: ide/UFFim
        - dh_emi: ide/dhEmi
        - nprot: protMDFe/infProt/nProt (se existir)
        - cstat: protMDFe/infProt/cStat (se existir)
        - xmotivo: protMDFe/infProt/xMotivo (se existir)

    Args:
        xml_path: Caminho do arquivo XML.

    Returns:
        Dict com os campos acima, ou None se não for MDF-e válido/parseável.
    """
    try:
        xml_path = Path(xml_path)
        root = ET.parse(xml_path).getroot()
    except Exception as e:
        logger.error("[ERROR] Falha ao ler XML MDF-e (%s): %s", xml_path, e)
        return None

    ns = {
        "proc": "http://www.portalfiscal.inf.br/MDFe",
        "mdfe": "http://www.portalfiscal.inf.br/mdfe",
    }

    inf = root.find(".//mdfe:infMDFe", ns)
    if inf is None:
        logger.error("[ERROR] XML não parece MDF-e (infMDFe ausente): %s", xml_path)
        return None

    ide = inf.find("mdfe:ide", ns)
    emit = inf.find("mdfe:emit", ns)

    def _text(node: Optional[ET.Element], path: str) -> Optional[str]:
        if node is None:
            return None
        el = node.find(path, ns)
        if el is None or el.text is None:
            return None
        val = el.text.strip()
        return val or None

    # Preferir chave do protocolo (quando existe mdfeProc com protMDFe)
    ch_mdfe = _text(root, ".//proc:protMDFe/proc:infProt/proc:chCTe")
    if not ch_mdfe:
        # Fallback: atributo Id do infMDFe (formato "MDFe<chave>")
        inf_id = (inf.attrib.get("Id") or "").strip()
        if inf_id.startswith("MDFe"):
            ch_mdfe = inf_id.replace("MDFe", "", 1).strip() or None
            
    ch_ctes = []
    for el in root.findall(".//mdfe:infCTe/mdfe:chCTe", ns):
        if el.text:
            ch = el.text.strip()
            if ch:
                ch_ctes.append(ch)

    n_mdf = _text(ide, "mdfe:nMDF")  # seu XML usa nMDF
    dh_emi = _text(ide, "mdfe:dhEmi")
    uf_ini = _text(ide, "mdfe:UFIni")
    uf_fim = _text(ide, "mdfe:UFFim")

    nome_emit = _text(emit, "mdfe:xNome") or "sem_nome"
    cnpj_emit = _text(emit, "mdfe:CNPJ") or _text(emit, "mdfe:CPF")

    nprot = _text(root, ".//proc:protMDFe/proc:infProt/proc:nProt")
    cstat = _text(root, ".//proc:protMDFe/proc:infProt/proc:cStat")
    xmotivo = _text(root, ".//proc:protMDFe/proc:infProt/proc:xMotivo")

    if not ch_mdfe or not n_mdf:
        logger.error(
            "[ERROR] MDF-e sem campos essenciais (chMDFe=%r, nMDF=%r) em %s",
            ch_mdfe, n_mdf, xml_path,
        )
        return None

    return {
        "chCTes": ch_ctes,
        "chMDFe": ch_mdfe,
        "nMDFE": n_mdf,
        "dh_emi": dh_emi,
        "uf_ini": uf_ini,
        "uf_fim": uf_fim,
        "nome_emit": nome_emit,
        "cnpj_emit": cnpj_emit,
        "nprot": nprot,
        "cstat": cstat,
        "xmotivo": xmotivo,
    }

def processar_cancelamento_cte(xml_path):
    """Extrai os dados necessários de um XML de cancelamento de CT-e."""

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        chCTe = root.find(".//cte:chCTe", NS)
        chCTe = chCTe.text if chCTe is not None else None

        tpEvento = root.find(".//cte:tpEvento", NS)
        tpEvento = tpEvento.text if tpEvento is not None else None

        motivo = root.find(".//cte:xJust", NS)
        motivo = motivo.text if motivo is not None else "Cancelamento do CT-e"

        cStat = root.find(".//cte:cStat", NS)
        cStat = cStat.text if cStat is not None else None

        xMotivo = root.find(".//cte:xMotivo", NS)
        xMotivo = xMotivo.text if xMotivo is not None else None

        if not chCTe:
            raise ValueError("XML de cancelamento sem chave do CT-e.")

        if tpEvento != "110111":
            raise ValueError(f"XML não é evento de cancelamento de CT-e (tpEvento={tpEvento}).")

        if cStat not in {"135", "136", "155"}:
            raise ValueError(f"Cancelamento não autorizado (cStat={cStat}, xMotivo={xMotivo}).")

        return {
            "chCTe": chCTe,
            "motivo": motivo
        }

    except ET.ParseError:
        logger.error("Erro ao processar XML de cancelamento.")
        return None

    except Exception as e:
        logger.error("Erro ao processar cancelamento: %s", e)
        return None
    

def processar_todos_ctes():
    """Processa todos os arquivos XML de CT-e na pasta XML_DIR e retorna lista de dicionários."""
    resultados = []

    if not os.path.exists(XML_DIR):
        logger.warning(f"[AVISO] Pasta '{XML_DIR}' não encontrada.")
        return resultados  # retorna lista vazia

    for file_name in os.listdir(XML_DIR):
        if file_name.lower().endswith(".xml"):
            xml_path = os.path.join(XML_DIR, file_name)
            try:
                dados = processar_cte(xml_path)
                resultados.append(dados)
                logger.info(f"\n[OK] Arquivo: {file_name}")
                logger.info(dados)
            except Exception as e:
                logger.error(f"[ERRO] Falha ao processar '{file_name}': {e}")


    return resultados



