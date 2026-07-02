# Bill Gen

Automação em Python para monitorar emails de CT-e, MDF-e e cancelamentos, gerar boletos via API do Banco Inter, arquivar documentos fiscais e preparar XMLs de MDF-e para averbação.

## Funcionalidades

- Leitura de emails via IMAP para CT-e, MDF-e e cancelamentos.
- Extração de dados de XML de CT-e/MDF-e.
- Criação, download e envio de boleto pelo Banco Inter.
- Persistência do código de solicitação do boleto em PostgreSQL.
- Cancelamento de boleto quando chegar evento autorizado de cancelamento de CT-e.
- Arquivamento final por chave do MDF-e, com CT-es, boletos e MDF-e na mesma pasta.
- Cópia do XML de MDF-e para a pasta de averbação.

## Estrutura

```text
.
├── data/
│   ├── db_bill.py
├── src/
│   ├── bank_API.py
│   ├── email_monitor.py
│   ├── xml_reader.py
│   ├── averbe_porto.py
│   └── config.py
├── main.py
├── requirements.txt
└── README.md
```

Pastas com documentos fiscais, XMLs, PDFs, certificados, logs, builds e variáveis sensíveis ficam fora do Git por segurança.

## Requisitos

- Python 3.11 ou superior.
- PostgreSQL acessível pela rede.
- Conta e credenciais da API do Banco Inter.
- Certificado e chave da API Inter na pasta local `credentials/`.
- Conta de email com acesso IMAP/SMTP habilitado.

## Instalação

Crie e ative um ambiente virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Instale as dependências:

```powershell
pip install -r requirements.txt
```

## Configuração

Crie um arquivo `.env` na raiz do projeto. Esse arquivo não deve ser enviado ao GitHub.

Variáveis usadas pelo projeto:

```env
EMAIL_USER=
EMAIL_PASS=

CLIENT_ID=
CLIENT_SECRET=
ACCOUNT_ID=

DB_USER=
DB_PASS=

PORTO_USER=
PORTO_PASS=
AVERBE_COMP=5
```

Os certificados da API Inter devem existir localmente em:

```text
credentials/Inter API_Certificado.crt
credentials/Inter API_Chave.key
```

## Execução

Com o ambiente virtual ativo:

```powershell
python main.py
```

O monitor busca emails não lidos de CT-e, MDF-e e cancelamento. Ao processar MDF-e, os arquivos relacionados ficam agrupados em:

```text
data/documentos/<chave_mdfe>/
```

## Segurança

Nunca envie ao GitHub:

- `.env` ou qualquer arquivo com senha/token.
- Certificados, chaves privadas ou arquivos `.crt`, `.key`, `.pem`, `.pfx`.
- XMLs/PDFs de CT-e, MDF-e, boletos ou documentos fiscais reais.
- Logs de execução.