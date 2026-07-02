import os
import psycopg2
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
# Configuração básica de log
logger = logging.getLogger(__name__)


class BillRepository:
    '''
    Repositório para armazenar boletos gerados a partir de CT-e,
    contendo:
        - nCTe
        - código da solicitação do Banco Inter (codReq)
        - dados do destinatário
        - valor do serviço
        - expiração automática
    '''

    load_dotenv()
    def __init__(self,
                 dbname="bill_gen",
                 user=os.getenv("DB_USER"),
                 password=os.getenv("DB_PASS"),
                 host="192.168.3.8",
                 port=5432):
        
        self.db_config = {
            "dbname": dbname,
            "user": user,
            "password": password,
            "host": host,
            "port": port,
        }

        self._ensure_table_exists()


    def _connect(self):
        return psycopg2.connect(**self.db_config)


    def _ensure_table_exists(self):
        '''
        Cria a tabela `boletos` se ela não existir.
        Campos da Tabela:
            - id: Identificador único do registro (chave primária)
            - ncte: Número do CT-e (único)
            - codigo_solicitacao: Código de solicitação do Banco Inter
            - dest: Nome do destinatário
            - dest_cnpj: CNPJ do destinatário
            - valor_servico: Valor do serviço de transporte
            - chave_cte: Chave de acesso do CT-e
            - created_at: Data e hora de criação do registro
            - expires_at: Data e hora de expiração do registro
        '''
        query = """
        CREATE TABLE IF NOT EXISTS boletos (
            id SERIAL PRIMARY KEY,
            ncte INTEGER NOT NULL UNIQUE,
            codigo_solicitacao VARCHAR(80) NOT NULL,
            dest VARCHAR(255) NOT NULL,
            dest_cnpj VARCHAR(20) NOT NULL,
            valor_servico NUMERIC(12,2) NOT NULL,
            chave_cte VARCHAR(44),
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMP NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_boletos_ncte
        ON boletos (ncte);

        CREATE INDEX IF NOT EXISTS idx_boletos_chave_cte
        ON boletos (chave_cte);

        CREATE INDEX IF NOT EXISTS idx_boletos_expires
        ON boletos (expires_at);;
        """

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    conn.commit()
        except Exception as e:
            logger.exception("Falha ao criar tabela")


    def save_table(self, dados: dict, cod_req: str, validade_dias: int = 60):
        '''
        Salva ou atualiza os dados do boleto na tabela `boletos`.

        Parâmetros:
            dados (dict):
                nCTE (str): Número do CT-e.
                chCTe (str): Chave de acesso do CT-e.
                nome_dest (str): Nome do destinatário.
                cnpj_dest (str): CNPJ do destinatário.
                valor_servico (str | float): Valor do serviço de transporte.

            cod_req (str):
                Código de solicitação retornado pela API do Banco Inter.

            validade_dias (int, opcional):
                Quantidade de dias até a expiração e exclusão do registro.
                Padrão: 60.
        '''

        now = datetime.now()
        expires = now + timedelta(days=validade_dias)

        query = """
        INSERT INTO boletos 
        (ncte, codigo_solicitacao, dest, dest_cnpj, valor_servico, chave_cte, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ncte) DO UPDATE SET
            codigo_solicitacao = EXCLUDED.codigo_solicitacao,
            dest = EXCLUDED.dest,
            dest_cnpj = EXCLUDED.dest_cnpj,
            valor_servico = EXCLUDED.valor_servico,
            chave_cte = EXCLUDED.chave_cte,
            created_at = EXCLUDED.created_at,
            expires_at = EXCLUDED.expires_at;
        """

        params = (
            dados["nCTE"],
            cod_req,
            dados["nome_dest"],
            dados["cnpj_dest"],
            float(dados["valor_servico"]),
            dados["chCTe"],
            now,
            expires
        )

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    conn.commit()
                    logger.info(f"[OK] Registro salvo para CT-e {dados['nCTE']} (expira em {expires})")
        except Exception as e:
            logger.error(f"[ERROR] Falha ao salvar na tabela: {e}")


    def get_code_by_ncte(self, n_cte: str):
        '''
        Retorna um código válido (não expirado) para o CT-e.

        '''

        query = """
        SELECT codigo_solicitacao
        FROM boletos
        WHERE ncte = %s
          AND expires_at > NOW()
        LIMIT 1;
        """

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (n_cte,))
                    result = cur.fetchone()
                    return result[0] if result else None

        except Exception as e:
            logger.error(f"[ERROR] Erro ao buscar código do CT-e {n_cte}: {e}")
            return None

    def get_code_by_chcte(self, ch_cte: str):
        '''
        Retorna um código válido (não expirado) para o CT-e.

        Parâmetros:
            ch_cte (str): Chave de acesso do CT-e.
        '''

        query = '''
        SELECT codigo_solicitacao
        FROM boletos
        WHERE chave_cte = %s
          AND expires_at > NOW()
        LIMIT 1;
        '''

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (ch_cte,))
                    result = cur.fetchone()
                    return result[0] if result else None

        except Exception as e:
            logger.error(f"[ERROR] Erro ao buscar código do CT-e {ch_cte}: {e}")
            return None

    def clean_expired(self):
        '''
        Remove boletos cujo prazo expirou.
        '''
        query = "DELETE FROM boletos WHERE expires_at <= NOW();"

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(query)
                    removidos = cur.rowcount
                    conn.commit()
                    logger.info(f"[INFO] Registros expirados removidos: {removidos}")
        except Exception as e:
            logger.error(f"[ERROR] Falha ao remover registros expirados: {e}")
