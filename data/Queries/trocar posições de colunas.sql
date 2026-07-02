BEGIN;

ALTER TABLE boletos RENAME TO boletos_old;

CREATE TABLE boletos (
    id SERIAL PRIMARY KEY,
    ncte VARCHAR(20) NOT NULL UNIQUE,
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

INSERT INTO boletos (
    id,
    ncte,
    codigo_solicitacao,
    dest,
    dest_cnpj,
    valor_servico,
    chave_cte,
    created_at,
    expires_at
)
SELECT
    id,
    ncte,
    codigo_solicitacao,
    dest,
    dest_cnpj,
    valor_servico,
    chave_cte,
    created_at,
    expires_at
FROM boletos_old;

SELECT setval(
    pg_get_serial_sequence('boletos', 'id'),
    (SELECT MAX(id) FROM boletos)
);

DROP TABLE boletos_old;

COMMIT;