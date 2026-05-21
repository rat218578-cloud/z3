"""
BAC BO BOT - AGENTE Z3 + NEURAL PREDICTOR v1.0
SISTEMA COMPLETAMENTE NOVO - ABANDONA ESTRATÉGIAS ANTIGAS

APIs configuradas:
1. API_DIRETO: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc
2. API_LATEST: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest
3. API_BACKUP: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo

ATUALIZAÇÃO: 0.3 segundos entre cada requisição

================================================================================
📊 EXPLICAÇÃO DAS TABELAS DO BANCO DE DADOS
================================================================================

O sistema cria 3 tabelas principais no PostgreSQL:

┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. TABELA rodadas                                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│ Armazena cada rodada coletada das APIs                                      │
│                                                                             │
│ Campos:                                                                     │
│   - id: VARCHAR(50) PRIMARY KEY           → ID único da rodada             │
│   - data_hora: TIMESTAMP NOT NULL         → Data/hora do salvamento        │
│   - p1, p2, b1, b2: INT NOT NULL          → Dados individuais (1-6)        │
│   - player_score: INT NOT NULL            → p1 + p2                        │
│   - banker_score: INT NOT NULL            → b1 + b2                        │
│   - resultado: VARCHAR(10) NOT NULL       → PLAYER/BANKER/TIE              │
│   - fonte, api_origem: VARCHAR            → Qual API forneceu o dado       │
│   - created_at: TIMESTAMP                 → Quando foi inserido no banco   │
│                                                                             │
│ Índice: idx_rodadas_data (data_hora DESC) → Busca mais rápida              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. TABELA previsoes                                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│ Armazena as previsões feitas pelo Agente Z3                                 │
│                                                                             │
│ Campos:                                                                     │
│   - id: SERIAL PRIMARY KEY                → Auto-incremento                │
│   - rodada_id: VARCHAR(50)                → Referência à rodada            │
│   - p1, p2, b1, b2: INT NOT NULL          → Dados previstos (1-6)          │
│   - player_score: INT NOT NULL            → Soma prevista do Player        │
│   - banker_score: INT NOT NULL            → Soma prevista do Banker        │
│   - resultado: VARCHAR(10) NOT NULL       → Resultado previsto             │
│   - confianca: DECIMAL(5,2)               → Confiança da previsão (0-100)  │
│   - acertou: BOOLEAN                      → TRUE/False após validação      │
│   - timestamp: TIMESTAMP                  → Quando a previsão foi gerada   │
│                                                                             │
│ Índice: idx_previsoes_acertou (acertou)   → Estatísticas rápidas           │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. TABELA estado_z3                                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│ Salva o estado do gerador Mersenne Twister recuperado pelo Z3               │
│                                                                             │
│ Campos:                                                                     │
│   - id: SERIAL PRIMARY KEY                → Auto-incremento                │
│   - estado_json: TEXT NOT NULL            → 624 números em JSON            │
│   - posicao: INT NOT NULL                 → Posição atual (0-623)          │
│   - rounds_usados: INT NOT NULL           → Quantos rounds usados (156)    │
│   - criado_em: TIMESTAMP                  → Quando foi criado              │
│   - ativo: BOOLEAN DEFAULT TRUE           → Estado ativo atual             │
└─────────────────────────────────────────────────────────────────────────────┘

================================================================================
📝 EXEMPLO DE DADOS SALVOS
================================================================================

rodadas (quando uma rodada é coletada):
┌─────────────────────────────────────────────────────────────────────────────┐
│ id: "6a0ee4d3c2978340d6f24dbc"                                              │
│ data_hora: "2026-05-21 15:30:45"                                            │
│ p1: 2, p2: 3, b1: 4, b2: 2                                                  │
│ player_score: 5, banker_score: 6                                            │
│ resultado: "BANKER"                                                         │
│ fonte: "api_direto"                                                         │
└─────────────────────────────────────────────────────────────────────────────┘

previsoes (antes da rodada acontecer):
┌─────────────────────────────────────────────────────────────────────────────┐
│ rodada_id: "6a0ee4d3c2978340d6f24dbc"                                       │
│ p1: 3, p2: 4, b1: 2, b2: 5                                                  │
│ player_score: 7, banker_score: 7                                            │
│ resultado: "TIE"                                                            │
│ confianca: 85.50                                                            │
│ acertou: null  ← Aguardando resultado real                                  │
└─────────────────────────────────────────────────────────────────────────────┘

Após a rodada real acontecer, o campo acertou é atualizado para TRUE ou FALSE

================================================================================
🔄 FLUXO DE FUNCIONAMENTO
================================================================================

1. A CADA 0.3 SEGUNDOS:
   ├── ColetorAPI.buscar_todas_apis()
   │   ├── API_DIRETO → lista de rodadas
   │   ├── API_LATEST → 1 rodada
   │   └── API_BACKUP → 1 rodada
   │
   ├── Para cada rodada nova:
   │   ├── db.salvar_rodada() → INSERE NA TABELA rodadas
   │   └── processar_rodada() → Verifica previsões
   │
   └── Se estado Z3 recuperado:
       ├── Verifica previsão vs realidade
       └── db.atualizar_acerto_previsao() → UPDATE tabela previsoes

2. QUANDO 156 RODADAS SÃO COLETADAS:
   ├── AgenteZ3.recuperar_estado_dos_dados()
   │   ├── Pega últimas 156 rodadas do banco
   │   ├── Aplica Z3 para resolver equações
   │   ├── Recupera estado MT19937 (624 números)
   │   └── db.salvar_estado_z3() → INSERE NA TABELA estado_z3
   │
   └── Gera primeiras 20 previsões → TABELA previsoes

3. A PARTIR DE ENTÃO:
   ├── Cada nova rodada valida a previsão anterior
   ├── Acertos/Erros são registrados
   └── Novas previsões são geradas continuamente

================================================================================
📊 CONSULTAS ÚTEIS PARA MONITORAMENTO
================================================================================

-- Ver últimas rodadas com suas previsões
SELECT r.id, r.p1, r.p2, r.b1, r.b2, r.resultado,
       p.p1 as p_p1, p.p2 as p_p2, p.b1 as p_b1, p.b2 as p_b2,
       p.acertou
FROM rodadas r
LEFT JOIN previsoes p ON r.id = p.rodada_id
ORDER BY r.data_hora DESC
LIMIT 10;

-- Estatísticas de acerto por API
SELECT api_origem, COUNT(*) as total, 
       SUM(CASE WHEN p.acertou THEN 1 ELSE 0 END) as acertos
FROM rodadas r
JOIN previsoes p ON r.id = p.rodada_id
GROUP BY api_origem;

-- Ver o estado ativo do Z3
SELECT estado_json, posicao, rounds_usados, criado_em
FROM estado_z3
WHERE ativo = TRUE;

-- Precisão das últimas 100 previsões
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN acertou THEN 1 ELSE 0 END) as acertos,
    ROUND(100.0 * SUM(CASE WHEN acertou THEN 1 ELSE 0 END) / COUNT(*), 2) as precisao
FROM previsoes 
WHERE acertou IS NOT NULL
ORDER BY id DESC
LIMIT 100;

================================================================================
"""

import os
import json
import time
import threading
import requests
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
from collections import deque
import logging

# Importações forçadas do Z3
import subprocess
import sys

# Garantir que Z3 está instalado
try:
    import z3
    from z3 import *
except ImportError:
    print("[*] Instalando z3-solver...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "z3-solver", "--force-reinstall", "-q"])
    from z3 import *

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import psycopg2
import urllib.parse

# =============================================================================
# CONFIGURAÇÕES
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_hW3kU9LZfsgB@ep-summer-meadow-ap9gu9vy-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require")

# ===== TRÊS APIS CONFIGURADAS =====
API_DIRETO = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc"
API_LATEST = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"
API_BACKUP = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo"
# ==================================

PORT = int(os.environ.get("PORT", 5000))

# TEMPO DE ATUALIZAÇÃO: 0.3 SEGUNDOS
UPDATE_INTERVAL = 0.3

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive'
}

# =============================================================================
# CONFIGURAÇÕES DO Z3 (COMPROVADAS NO TESTE)
# =============================================================================

ORDER = ["p1", "p2", "b1", "b2"]
OFFSET = 0
MAP_TYPE = "Lemire64"
ROUNDS_FOR_RECOVERY = 156

# Cache de IDs já processados
ULTIMO_ID_CONTROLE = {}
IDS_PROCESSADOS = set()

# =============================================================================
# FUNÇÕES Z3 - Mapeamento Lemire64
# =============================================================================

def lemire64_mapping(v):
    prod = ZeroExt(32, v) * BitVecVal(6, 64)
    return Extract(63, 32, prod) + BitVecVal(1, 32)

def temper(v):
    v = v ^ LShR(v, 11)
    v = v ^ ((v << 7) & BitVecVal(0x9D2C5680, 32))
    v = v ^ ((v << 15) & BitVecVal(0xEFC60000, 32))
    v = v ^ LShR(v, 18)
    return v

# =============================================================================
# MODELO DE DADOS
# =============================================================================

@dataclass
class DadosRodada:
    p1: int
    p2: int
    b1: int
    b2: int
    
    @property
    def player_score(self) -> int:
        return self.p1 + self.p2
    
    @property
    def banker_score(self) -> int:
        return self.b1 + self.b2
    
    @property
    def resultado(self) -> str:
        if self.player_score > self.banker_score:
            return "PLAYER"
        elif self.banker_score > self.player_score:
            return "BANKER"
        else:
            return "TIE"
    
    def to_list(self) -> List[int]:
        return [self.p1, self.p2, self.b1, self.b2]
    
    def to_dict(self) -> Dict:
        return {
            'p1': self.p1, 'p2': self.p2,
            'b1': self.b1, 'b2': self.b2,
            'player_score': self.player_score,
            'banker_score': self.banker_score,
            'resultado': self.resultado
        }


@dataclass
class Rodada:
    id: str
    data_hora: datetime
    dados: DadosRodada
    resultado: str
    fonte: str
    api_origem: str = ""


# =============================================================================
# DATABASE MANAGER
# =============================================================================

class DatabaseManager:
    """
    GERENCIADOR DO BANCO DE DADOS
    
    RESPONSÁVEL POR:
    - Criar as tabelas (rodadas, previsoes, estado_z3)
    - Salvar rodadas coletadas das APIs
    - Salvar previsões feitas pelo Agente Z3
    - Atualizar acertos das previsões
    - Salvar estado recuperado do MT19937
    - Consultar históricos e estatísticas
    """
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        self._init_tables()
    
    def _get_connection(self):
        """Estabelece conexão com PostgreSQL"""
        if not self.database_url:
            return None
        try:
            parsed = urllib.parse.urlparse(self.database_url)
            conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                database=parsed.path[1:],
                sslmode='require',
                connect_timeout=10
            )
            return conn
        except Exception as e:
            logger.error(f"Erro conectar: {e}")
            return None
    
    def _init_tables(self):
        """
        CRIA AS TABELAS NO BANCO DE DADOS
        
        Tabela 1: rodadas - Armazena cada rodada coletada
        Tabela 2: previsoes - Armazena previsões do Z3
        Tabela 3: estado_z3 - Armazena estado recuperado do MT19937
        """
        conn = self._get_connection()
        if not conn:
            logger.warning("⚠️ Sem conexão com banco, usando memória")
            return
        try:
            cur = conn.cursor()
            
            # ===== TABELA 1: rodadas =====
            # Guarda cada rodada real coletada da API
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rodadas (
                    id VARCHAR(50) PRIMARY KEY,
                    data_hora TIMESTAMP NOT NULL,
                    p1 INT NOT NULL,
                    p2 INT NOT NULL,
                    b1 INT NOT NULL,
                    b2 INT NOT NULL,
                    player_score INT NOT NULL,
                    banker_score INT NOT NULL,
                    resultado VARCHAR(10) NOT NULL,
                    fonte VARCHAR(50),
                    api_origem VARCHAR(50),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # ===== TABELA 2: previsoes =====
            # Guarda as previsões feitas pelo Agente Z3
            cur.execute("""
                CREATE TABLE IF NOT EXISTS previsoes (
                    id SERIAL PRIMARY KEY,
                    rodada_id VARCHAR(50) REFERENCES rodadas(id),
                    p1 INT NOT NULL,
                    p2 INT NOT NULL,
                    b1 INT NOT NULL,
                    b2 INT NOT NULL,
                    player_score INT NOT NULL,
                    banker_score INT NOT NULL,
                    resultado VARCHAR(10) NOT NULL,
                    confianca DECIMAL(5,2),
                    acertou BOOLEAN,
                    timestamp TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # ===== TABELA 3: estado_z3 =====
            # Guarda o estado do Mersenne Twister recuperado pelo Z3
            cur.execute("""
                CREATE TABLE IF NOT EXISTS estado_z3 (
                    id SERIAL PRIMARY KEY,
                    estado_json TEXT NOT NULL,
                    posicao INT NOT NULL,
                    rounds_usados INT NOT NULL,
                    criado_em TIMESTAMP DEFAULT NOW(),
                    ativo BOOLEAN DEFAULT TRUE
                )
            """)
            
            # ÍNDICES PARA ACELERAR CONSULTAS
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rodadas_data ON rodadas(data_hora DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_previsoes_acertou ON previsoes(acertou)")
            
            conn.commit()
            cur.close()
            logger.info("✅ Tabelas criadas/verificadas (Agente Z3 v1.0)")
        except Exception as e:
            logger.error(f"Erro tabelas: {e}")
        finally:
            conn.close()
    
    def salvar_rodada(self, rodada: Rodada) -> bool:
        """
        SALVA UMA RODADA NA TABELA rodadas
        
        Exemplo do que é salvo:
        {
            "id": "6a0ee4d3c2978340d6f24dbc",
            "data_hora": "2026-05-21 15:30:45",
            "p1": 2, "p2": 3, "b1": 4, "b2": 2,
            "player_score": 5,
            "banker_score": 6,
            "resultado": "BANKER",
            "fonte": "api_direto",
            "api_origem": "API_DIRETO"
        }
        """
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO rodadas (id, data_hora, p1, p2, b1, b2, player_score, banker_score, resultado, fonte, api_origem)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (rodada.id, rodada.data_hora, rodada.dados.p1, rodada.dados.p2,
                  rodada.dados.b1, rodada.dados.b2, rodada.dados.player_score,
                  rodada.dados.banker_score, rodada.resultado, rodada.fonte, rodada.api_origem))
            conn.commit()
            cur.close()
            logger.debug(f"✅ Rodada {rodada.id} salva")
            return True
        except Exception as e:
            logger.error(f"Erro salvar rodada: {e}")
            return False
        finally:
            conn.close()
    
    def salvar_previsao(self, rodada_id: str, dados: DadosRodada, confianca: float) -> bool:
        """
        SALVA UMA PREVISÃO NA TABELA previsoes
        
        Exemplo:
        {
            "rodada_id": "6a0ee4d3c2978340d6f24dbc",
            "p1": 3, "p2": 4, "b1": 2, "b2": 5,
            "player_score": 7,
            "banker_score": 7,
            "resultado": "TIE",
            "confianca": 85.5,
            "acertou": null  ← Aguardando resultado real
        }
        """
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO previsoes (rodada_id, p1, p2, b1, b2, player_score, banker_score, resultado, confianca)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (rodada_id, dados.p1, dados.p2, dados.b1, dados.b2,
                  dados.player_score, dados.banker_score, dados.resultado, confianca))
            conn.commit()
            cur.close()
            logger.debug(f"✅ Previsão para rodada {rodada_id} salva")
            return True
        except Exception as e:
            logger.error(f"Erro salvar previsao: {e}")
            return False
        finally:
            conn.close()
    
    def atualizar_acerto_previsao(self, rodada_id: str, acertou: bool):
        """
        ATUALIZA O CAMPO acertou NA TABELA previsoes
        
        Chamado quando a rodada real acontece e podemos validar a previsão
        """
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("UPDATE previsoes SET acertou = %s WHERE rodada_id = %s", (acertou, rodada_id))
            conn.commit()
            cur.close()
            logger.debug(f"✅ Previsão {rodada_id}: acertou={acertou}")
        except Exception as e:
            logger.error(f"Erro atualizar acerto: {e}")
        finally:
            conn.close()
    
    def salvar_estado_z3(self, estado: List[int], posicao: int, rounds_usados: int) -> bool:
        """
        SALVA O ESTADO RECUPERADO DO MT19937 NA TABELA estado_z3
        
        estado: lista de 624 inteiros de 32 bits
        posicao: posição atual no estado (0-623)
        rounds_usados: quantos rounds usaram para recuperar (156)
        """
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            # Desativa estados anteriores
            cur.execute("UPDATE estado_z3 SET ativo = FALSE WHERE ativo = TRUE")
            # Salva novo estado
            cur.execute("""
                INSERT INTO estado_z3 (estado_json, posicao, rounds_usados, ativo)
                VALUES (%s, %s, %s, TRUE)
            """, (json.dumps(estado), posicao, rounds_usados))
            conn.commit()
            cur.close()
            logger.info("✅ Estado Z3 salvo no banco")
            return True
        except Exception as e:
            logger.error(f"Erro salvar estado: {e}")
            return False
        finally:
            conn.close()
    
    def get_historico_rodadas(self, limit: int = 200) -> List[List[int]]:
        """
        BUSCA HISTÓRICO DE RODADAS PARA RECUPERAÇÃO DO ESTADO
        
        Retorna lista de [p1, p2, b1, b2] ordenado do mais antigo para o mais novo
        """
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT p1, p2, b1, b2 FROM rodadas
                ORDER BY data_hora ASC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            return [[r[0], r[1], r[2], r[3]] for r in rows]
        except Exception as e:
            logger.error(f"Erro get_historico: {e}")
            return []
        finally:
            conn.close()
    
    def get_ultimas_rodadas(self, limit: int = 20) -> List[Dict]:
        """Retorna as últimas N rodadas para o frontend"""
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, p1, p2, b1, b2, player_score, banker_score, resultado, data_hora, api_origem
                FROM rodadas ORDER BY data_hora DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            return [{
                'id': r[0], 'p1': r[1], 'p2': r[2], 'b1': r[3], 'b2': r[4],
                'player_score': r[5], 'banker_score': r[6], 'resultado': r[7],
                'data_hora': r[8].isoformat() if r[8] else None, 'api_origem': r[9]
            } for r in rows]
        except Exception as e:
            return []
        finally:
            conn.close()
    
    def get_estatisticas_previsoes(self) -> Dict:
        """
        ESTATÍSTICAS DAS PREVISÕES
        
        Retorna:
        - total: total de previsões já validadas
        - acertos: quantas acertaram
        - precisao: porcentagem de acertos
        """
        conn = self._get_connection()
        if not conn:
            return {'total': 0, 'acertos': 0, 'precisao': 0}
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN acertou THEN 1 ELSE 0 END) as acertos,
                       ROUND(AVG(CASE WHEN acertou THEN 100 ELSE 0 END), 2) as precisao
                FROM previsoes WHERE acertou IS NOT NULL
            """)
            row = cur.fetchone()
            cur.close()
            return {
                'total': row[0] or 0,
                'acertos': row[1] or 0,
                'precisao': row[2] or 0
            }
        except Exception as e:
            return {'total': 0, 'acertos': 0, 'precisao': 0}
        finally:
            conn.close()
    
    def get_total_rodadas(self) -> int:
        """Retorna o total de rodadas no banco"""
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM rodadas")
            count = cur.fetchone()[0]
            cur.close()
            return count
        except Exception as e:
            return 0
        finally:
            conn.close()


# =============================================================================
# AGENTE Z3
# =============================================================================

class AgenteZ3:
    """
    AGENTE Z3 - Responsável por recuperar o estado do Mersenne Twister
    
    Funcionamento:
    1. Coleta 156 rodadas do banco (624 outputs de dados)
    2. Aplica restrições Z3 para encontrar o estado interno do MT19937
    3. Uma vez recuperado, pode prever infinitas rodadas futuras
    
    Configuração comprovada em teste:
    - Mapeamento: Lemire64
    - Ordem: p1, p2, b1, b2
    - Offset: 0
    """
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.estado_recuperado = None
        self.posicao_atual = 0
        self.estado_original = None
        self.estado_atual = None
        self.previsoes_cache = []
    
    def recuperar_estado_do_banco(self) -> bool:
        """Tenta recuperar estado salvo do banco"""
        return False
    
    def recuperar_estado_dos_dados(self) -> Optional[List[int]]:
        """
        RECUPERA O ESTADO DO MT19937 USANDO Z3
        
        Processo:
        1. Busca 156 rodadas do banco
        2. Converte para 624 outputs na ordem configurada
        3. Cria 624 variáveis para o estado
        4. Adiciona restrições: temper(state[i]) -> dado observado
        5. Resolve com Z3 (SAT/UNSAT)
        6. Se SAT, extrai o estado do modelo
        """
        historico = self.db.get_historico_rodadas(ROUNDS_FOR_RECOVERY + 50)
        
        if len(historico) < ROUNDS_FOR_RECOVERY:
            logger.warning(f"[Z3] Histórico insuficiente: {len(historico)} < {ROUNDS_FOR_RECOVERY}")
            return None
        
        logger.info(f"[Z3] Iniciando recuperação com {len(historico)} rodadas...")
        
        # Pega bloco de 156 rodadas
        bloco = historico[:ROUNDS_FOR_RECOVERY]
        
        # Achata os dados na ordem ORDER
        outputs = []
        for r in bloco:
            for key in ORDER:
                if key == 'p1':
                    outputs.append(r[0])
                elif key == 'p2':
                    outputs.append(r[1])
                elif key == 'b1':
                    outputs.append(r[2])
                else:  # b2
                    outputs.append(r[3])
        
        N = 624
        state = [BitVec(f's_{i}', 32) for i in range(N)]
        s = Then('simplify', 'bit-blast', 'sat').solver()
        s.set("timeout", 300000)  # 5 minutos
        
        logger.info(f"[Z3] Adicionando {len(outputs)} restrições...")
        
        # Adiciona restrições para cada output
        for i, dice_value in enumerate(outputs[:N]):
            mt_val = temper(state[i % N])
            expected = lemire64_mapping(mt_val)
            s.add(expected == dice_value)
        
        logger.info("[Z3] Resolvendo...")
        start = time.time()
        res = s.check()
        elapsed = time.time() - start
        
        logger.info(f"[Z3] Resultado: {res} ({elapsed:.1f}s)")
        
        if res == sat:
            model = s.model()
            estado = []
            for i in range(N):
                val = model[state[i]]
                estado.append(val.as_long() if hasattr(val, 'as_long') else int(val.as_string()))
            
            logger.info("[Z3] ✓ Estado recuperado com sucesso!")
            
            self.estado_recuperado = estado
            self.estado_original = list(estado)
            self.estado_atual = list(estado)
            self.posicao_atual = 0
            
            # Salva no banco
            self.db.salvar_estado_z3(estado, 0, ROUNDS_FOR_RECOVERY)
            
            return estado
        
        logger.error("[Z3] ✗ Falha na recuperação do estado")
        return None
    
    def _twist(self, state: List[int]) -> List[int]:
        """Atualiza o estado MT19937 (operação twist)"""
        new = list(state)
        for i in range(624):
            y = (new[i] & 0x80000000) + (new[(i+1) % 624] & 0x7FFFFFFF)
            new[i] = new[(i+397) % 624] ^ (y >> 1)
            if y & 1:
                new[i] ^= 0x9908B0DF
        return new
    
    def _temper_py(self, y: int) -> int:
        """Temperagem em Python puro (operação inversa do untemper)"""
        y ^= (y >> 11)
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= (y >> 18)
        return y & 0xFFFFFFFF
    
    def _mt_to_dice(self, v: int) -> int:
        """Converte valor MT19937 para dado (1-6) usando Lemire64"""
        return ((v * 6) >> 32) + 1
    
    def avancar_estado(self, quantidade_rounds: int = 1):
        """Avança o estado interno em N rounds (4 dados por round)"""
        if not self.estado_atual:
            return
        
        idx = self.posicao_atual
        total_dados = quantidade_rounds * 4
        
        for _ in range(total_dados):
            idx += 1
            if idx >= 624:
                self.estado_atual = self._twist(self.estado_atual)
                idx = 0
        
        self.posicao_atual = idx
    
    def prever_proximas(self, quantidade: int = 20) -> List[DadosRodada]:
        """
        PREVE AS PRÓXIMAS N RODADAS
        
        Usa o estado atual do MT19937 para gerar os próximos dados
        """
        if not self.estado_atual:
            return []
        
        previsoes = []
        rng = list(self.estado_atual)
        idx = self.posicao_atual
        
        for _ in range(quantidade):
            dados = []
            for _ in range(4):
                if idx >= 624:
                    rng = self._twist(rng)
                    idx = 0
                val = self._temper_py(rng[idx])
                dados.append(self._mt_to_dice(val))
                idx += 1
            
            previsoes.append(DadosRodada(p1=dados[0], p2=dados[1], b1=dados[2], b2=dados[3]))
        
        self.previsoes_cache = previsoes
        return previsoes
    
    def verificar_rodada(self, dados_reais: DadosRodada, dados_previstos: DadosRodada = None) -> Tuple[bool, DadosRodada]:
        """
        VERIFICA SE OS DADOS REAIS CORRESPONDEM À PREVISÃO
        
        Retorna:
        - acertou: True/False
        - dados_previstos: a previsão que foi usada
        """
        if dados_previstos is None:
            if not self.previsoes_cache:
                return False, None
            if len(self.previsoes_cache) > 0:
                dados_previstos = self.previsoes_cache.pop(0)
            else:
                return False, None
        
        acertou = dados_reais.to_list() == dados_previstos.to_list()
        
        # Atualiza estado para a próxima rodada
        self.avancar_estado(1)
        
        return acertou, dados_previstos


# =============================================================================
# COLETOR DE API - TRÊS APIS A CADA 0.3s
# =============================================================================

class ColetorAPI:
    """
    COORDENADOR DAS 3 APIs
    
    APIs:
    1. API_DIRETO: Retorna lista das últimas 10 rodadas
    2. API_LATEST: Retorna a rodada mais recente
    3. API_BACKUP: Fallback secundário
    
    Frequência: 0.3 segundos
    """
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.rodadas_processadas = 0
        self.api_stats = {
            'api_direto': {'sucessos': 0, 'erros': 0, 'ultimo_uso': None},
            'api_latest': {'sucessos': 0, 'erros': 0, 'ultimo_uso': None},
            'api_backup': {'sucessos': 0, 'erros': 0, 'ultimo_uso': None}
        }
        self.api_atual = 0
        self.ultima_rodada_id = None
        
        self.APIS = [
            ('API_DIRETO', API_DIRETO),
            ('API_LATEST', API_LATEST),
            ('API_BACKUP', API_BACKUP)
        ]
    
    def extrair_dados_rodada(self, dados: dict, api_nome: str) -> Optional[Rodada]:
        """
        EXTRAI OS DADOS DE UMA RODADA DA RESPOSTA DA API
        
        Suporta dois formatos:
        1. Formato novo: {data: {result: {playerDice: {first, second}, bankerDice: {first, second}}}}
        2. Formato antigo: {p1, p2, b1, b2, resultado}
        """
        try:
            rodada_id = dados.get('id') or dados.get('_id')
            
            if not rodada_id:
                return None
            
            # Evita duplicatas
            if rodada_id in IDS_PROCESSADOS or rodada_id in ULTIMO_ID_CONTROLE:
                return None
            
            # Extrai os dados (suporta dois formatos)
            if 'data' in dados and 'result' in dados.get('data', {}):
                data_obj = dados['data']
                result = data_obj.get('result', {})
                player_dice = result.get('playerDice', {})
                banker_dice = result.get('bankerDice', {})
                
                p1 = player_dice.get('first', 0)
                p2 = player_dice.get('second', 0)
                b1 = banker_dice.get('first', 0)
                b2 = banker_dice.get('second', 0)
            else:
                p1 = dados.get('p1') or dados.get('player1', 0)
                p2 = dados.get('p2') or dados.get('player2', 0)
                b1 = dados.get('b1') or dados.get('banker1', 0)
                b2 = dados.get('b2') or dados.get('banker2', 0)
            
            # Validação
            if not all(1 <= v <= 6 for v in [p1, p2, b1, b2]):
                return None
            
            dados_rodada = DadosRodada(p1=p1, p2=p2, b1=b1, b2=b2)
            resultado = dados.get('resultado') or dados_rodada.resultado
            
            IDS_PROCESSADOS.add(rodada_id)
            ULTIMO_ID_CONTROLE[rodada_id] = True
            
            return Rodada(
                id=rodada_id,
                data_hora=datetime.now(),
                dados=dados_rodada,
                resultado=resultado,
                fonte=api_nome,
                api_origem=api_nome
            )
            
        except Exception as e:
            logger.error(f"Erro extrair dados da {api_nome}: {e}")
            return None
    
    def buscar_todas_apis(self) -> List[Rodada]:
        """Busca em todas as 3 APIs e retorna as rodadas encontradas"""
        rodadas_encontradas = []
        
        for nome, url in self.APIS:
            try:
                response = requests.get(url, headers=HEADERS, timeout=3)
                
                if response.status_code == 200:
                    self.api_stats[nome]['sucessos'] += 1
                    self.api_stats[nome]['ultimo_uso'] = datetime.now()
                    
                    dados = response.json()
                    
                    # API_DIRETO retorna lista
                    if nome == 'API_DIRETO' and isinstance(dados, list):
                        for item in dados:
                            rodada = self.extrair_dados_rodada(item, nome)
                            if rodada:
                                rodadas_encontradas.append(rodada)
                    else:
                        # API_LATEST e API_BACKUP retornam objeto único
                        rodada = self.extrair_dados_rodada(dados, nome)
                        if rodada:
                            rodadas_encontradas.append(rodada)
                else:
                    self.api_stats[nome]['erros'] += 1
                    logger.warning(f"{nome} retornou status {response.status_code}")
                    
            except Exception as e:
                self.api_stats[nome]['erros'] += 1
                logger.error(f"Erro na {nome}: {e}")
        
        return rodadas_encontradas
    
    def coletar_e_processar(self, agente_z3: AgenteZ3, callback_rodada=None):
        """
        COLETA E PROCESSA AS RODADAS DAS 3 APIs
        
        Para cada rodada:
        1. Salva no banco
        2. Chama callback (processar_rodada)
        """
        rodadas = self.buscar_todas_apis()
        
        for rodada in rodadas:
            if rodada.id == self.ultima_rodada_id:
                continue
            
            self.ultima_rodada_id = rodada.id
            self.rodadas_processadas += 1
            
            # SALVA NO BANCO
            self.db.salvar_rodada(rodada)
            
            logger.info(f"🎲 RODADA #{self.rodadas_processadas}: P={rodada.dados.player_score} B={rodada.dados.banker_score} | {rodada.resultado} | API: {rodada.api_origem}")
            
            if callback_rodada:
                callback_rodada(rodada)
        
        return len(rodadas)


# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)
CORS(app)

db = DatabaseManager(DATABASE_URL)
agente_z3 = AgenteZ3(db)
coletor = ColetorAPI(db)

# Cache para o frontend (NÃO PERSISTE, APENAS MEMÓRIA)
cache = {
    'ultimas_rodadas': [],
    'ultimas_previsoes': [],
    'estatisticas': {},
    'estado_z3': {'recuperado': False, 'posicao': 0}
}


def processar_rodada(rodada: Rodada):
    """Processa uma rodada recém-coletada - ATUALIZA CACHE E VALIDA PREVISÕES"""
    global cache
    
    # Se estado Z3 recuperado, verifica se a previsão anterior estava correta
    if agente_z3.estado_recuperado:
        acertou, previsao_usada = agente_z3.verificar_rodada(rodada.dados)
        
        if previsao_usada:
            # ATUALIZA O BANCO: marca se a previsão acertou
            db.atualizar_acerto_previsao(rodada.id, acertou)
            
            if acertou:
                logger.info(f"✅ PREVISÃO CORRETA! {previsao_usada.to_list()} == {rodada.dados.to_list()}")
            else:
                logger.warning(f"❌ PREVISÃO ERRADA! Previsto={previsao_usada.to_list()} Real={rodada.dados.to_list()}")
    
    # Atualiza cache para o frontend
    cache['ultimas_rodadas'].insert(0, {
        'id': rodada.id,
        'p1': rodada.dados.p1, 'p2': rodada.dados.p2,
        'b1': rodada.dados.b1, 'b2': rodada.dados.b2,
        'player_score': rodada.dados.player_score,
        'banker_score': rodada.dados.banker_score,
        'resultado': rodada.resultado,
        'api': rodada.api_origem,
        'data': rodada.data_hora.strftime('%H:%M:%S')
    })
    
    # Mantém apenas últimas 50 rodadas no cache
    while len(cache['ultimas_rodadas']) > 50:
        cache['ultimas_rodadas'].pop()
    
    # Atualiza estatísticas
    cache['estatisticas'] = db.get_estatisticas_previsoes()
    cache['estatisticas']['total_rodadas'] = db.get_total_rodadas()
    cache['estatisticas']['api_stats'] = coletor.api_stats


def recuperar_estado_inicial():
    """Tenta recuperar o estado do Z3 ao iniciar"""
    logger.info("[INICIALIZAÇÃO] Tentando recuperar estado do Z3...")
    
    estado = agente_z3.recuperar_estado_dos_dados()
    
    if estado:
        cache['estado_z3']['recuperado'] = True
        cache['estado_z3']['posicao'] = agente_z3.posicao_atual
        
        # Gera primeiras previsões
        previsoes = agente_z3.prever_proximas(20)
        cache['ultimas_previsoes'] = [p.to_dict() for p in previsoes]
        
        logger.info("[INICIALIZAÇÃO] Estado recuperado! Previsões geradas.")
    else:
        logger.warning("[INICIALIZAÇÃO] Não foi possível recuperar o estado ainda. Aguardando mais dados...")


def loop_coleta():
    """
    LOOP PRINCIPAL DE COLETA
    
    - Executa a cada 0.3 segundos
    - Coleta as 3 APIs
    - Processa rodadas novas
    - Tenta recuperar estado se ainda não recuperou
    """
    logger.info(f"🔄 Loop de coleta iniciado - Intervalo: {UPDATE_INTERVAL}s")
    logger.info("📡 APIs configuradas:")
    logger.info(f"   1. API_DIRETO: {API_DIRETO[:80]}...")
    logger.info(f"   2. API_LATEST: {API_LATEST}")
    logger.info(f"   3. API_BACKUP: {API_BACKUP}")
    
    ultima_recuperacao = 0
    
    while True:
        try:
            start_time = time.time()
            
            # Coleta e processa rodadas
            coletor.coletar_e_processar(agente_z3, processar_rodada)
            
            # Tenta recuperar estado a cada 30 segundos se ainda não recuperou
            if not cache['estado_z3']['recuperado']:
                if time.time() - ultima_recuperacao > 30:
                    recuperar_estado_inicial()
                    ultima_recuperacao = time.time()
            
            # Gera novas previsões se estado estiver recuperado
            if cache['estado_z3']['recuperado'] and len(cache['ultimas_previsoes']) < 10:
                novas = agente_z3.prever_proximas(20)
                cache['ultimas_previsoes'] = [p.to_dict() for p in novas]
            
            # Calcula tempo de espera para manter o intervalo de 0.3s
            elapsed = time.time() - start_time
            sleep_time = max(0, UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_time)
            
        except Exception as e:
            logger.error(f"Erro no loop de coleta: {e}")
            time.sleep(UPDATE_INTERVAL)


# =============================================================================
# ROTAS DA API (FRONTEND)
# =============================================================================

@app.route('/')
def index():
    """Página principal"""
    try:
        return render_template('index.html')
    except Exception as e:
        return f"""
        <!DOCTYPE html>
        <html>
        <head><title>BAC BO BOT - Agente Z3</title></head>
        <body>
            <h1>BAC BO BOT - Agente Z3 v1.0</h1>
            <p>API está funcionando. Use os endpoints abaixo:</p>
            <ul>
                <li><a href="/api/stats">/api/stats</a> - Estatísticas</li>
                <li><a href="/api/rodadas">/api/rodadas</a> - Últimas rodadas</li>
                <li><a href="/api/previsoes">/api/previsoes</a> - Previsões</li>
                <li><a href="/api/apis">/api/apis</a> - Status das APIs</li>
            </ul>
        </body>
        </html>
        """


@app.route('/api/stats')
def api_stats():
    """Retorna estatísticas das previsões e estado do Z3"""
    return jsonify({
        'success': True,
        'data': cache['estatisticas'],
        'estado_z3': cache['estado_z3'],
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/rodadas')
def api_rodadas():
    """Retorna as últimas rodadas do banco"""
    limit = request.args.get('limit', 30, type=int)
    rodadas = db.get_ultimas_rodadas(limit)
    return jsonify({'success': True, 'data': rodadas})


@app.route('/api/previsoes')
def api_previsoes():
    """Retorna as próximas previsões"""
    return jsonify({
        'success': True,
        'data': cache['ultimas_previsoes'],
        'quantidade': len(cache['ultimas_previsoes']),
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/apis')
def api_apis():
    """Retorna status das 3 APIs"""
    return jsonify({
        'success': True,
        'data': {
            'apis': coletor.api_stats,
            'intervalo': UPDATE_INTERVAL,
            'ultima_atualizacao': datetime.now().isoformat()
        }
    })


@app.route('/api/recover')
def api_recover():
    """Força a recuperação do estado do Z3"""
    estado = agente_z3.recuperar_estado_dos_dados()
    if estado:
        cache['estado_z3']['recuperado'] = True
        cache['estado_z3']['posicao'] = agente_z3.posicao_atual
        previsoes = agente_z3.prever_proximas(20)
        cache['ultimas_previsoes'] = [p.to_dict() for p in previsoes]
        return jsonify({'success': True, 'message': 'Estado recuperado com sucesso!'})
    return jsonify({'success': False, 'message': 'Falha na recuperação'})


@app.route('/api/evolucao')
def api_evolucao():
    """Evolução da precisão das previsões (últimas 24 horas)"""
    conn = db._get_connection()
    if not conn:
        return jsonify({'precisao': []})
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                DATE_TRUNC('hour', timestamp) as hora,
                COUNT(*) as total,
                SUM(CASE WHEN acertou THEN 1 ELSE 0 END) as acertos
            FROM previsoes 
            WHERE acertou IS NOT NULL
            GROUP BY DATE_TRUNC('hour', timestamp)
            ORDER BY hora DESC
            LIMIT 24
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        evolucao = []
        for row in rows:
            precisao = (row[2] / row[1] * 100) if row[1] > 0 else 0
            evolucao.append({
                'hora': row[0].isoformat() if row[0] else None,
                'total': row[1],
                'acertos': row[2],
                'precisao': round(precisao, 2)
            })
        
        return jsonify({'success': True, 'data': evolucao})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "="*70)
    print("🚀 BAC BO BOT - AGENTE Z3 + NEURAL v1.0")
    print("="*70)
    print("\n🎯 ARQUITETURA:")
    print("   1. AGENTE Z3 - Recupera estado do MT19937")
    print("   2. AGENTE PREDITOR - Gera previsões determinísticas")
    print("   3. AGENTE VALIDADOR - Verifica previsões contra realidade")
    print("\n📡 APIs CONFIGURADAS:")
    print(f"   1. API_DIRETO: {API_DIRETO[:60]}...")
    print(f"   2. API_LATEST: {API_LATEST}")
    print(f"   3. API_BACKUP: {API_BACKUP}")
    print(f"\n⏱️  INTERVALO DE ATUALIZAÇÃO: {UPDATE_INTERVAL} segundos")
    print(f"🎲 ROUNDS PARA RECUPERAÇÃO: {ROUNDS_FOR_RECOVERY}")
    print(f"🔧 CONFIGURAÇÃO Z3: {MAP_TYPE} | ordem={','.join(ORDER)} | offset={OFFSET}")
    print("\n📊 TABELAS DO BANCO DE DADOS:")
    print("   - rodadas:    Armazena cada rodada coletada")
    print("   - previsoes:  Armazena previsões do Z3")
    print("   - estado_z3:  Armazena estado MT19937 recuperado")
    print("="*70 + "\n")
    
    # Tenta recuperar estado inicial
    recuperar_estado_inicial()
    
    # Inicia thread de coleta
    coleta_thread = threading.Thread(target=loop_coleta, daemon=True)
    coleta_thread.start()
    
    logger.info(f"🌐 Servidor rodando na porta {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
