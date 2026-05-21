"""
BAC BO BOT - AGENTE Z3 + NEURAL PREDICTOR v1.0
SISTEMA COMPLETAMENTE NOVO - ABANDONA ESTRATÉGIAS ANTIGAS

AGENTES IMPLEMENTADOS:
1. AGENTE Z3 - Recupera estado do MT19937 (individual)
2. AGENTE PREDITOR - Gera previsões determinísticas
3. AGENTE VALIDADOR - Verifica previsões contra realidade
4. AGENTE COLETIVO (EM MASSA) - Processa lotes de rodadas em paralelo

APIs configuradas:
1. API_DIRETO: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc
2. API_LATEST: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest
3. API_BACKUP: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo

ATUALIZAÇÃO: 0.3 segundos entre cada requisição
"""

import os
import json
import time
import threading
import requests
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# CONFIGURAÇÕES DO AGENTE EM MASSA
BATCH_SIZE = 50  # Tamanho do lote para processamento em massa
PARALLEL_WORKERS = 4  # Número de workers paralelos
VALIDATION_BATCH = 20  # Validar últimas 20 rodadas em lote

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


@dataclass
class ResultadoValidacao:
    """Resultado da validação em massa"""
    total_validados: int
    acertos: int
    erros: int
    precisao: float
    desvio_detectado: bool
    mensagem: str = ""


# =============================================================================
# DATABASE MANAGER
# =============================================================================

class DatabaseManager:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self._init_tables()
        self._init_massive_tables()
    
    def _get_connection(self):
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
        conn = self._get_connection()
        if not conn:
            logger.warning("⚠️ Sem conexão com banco, usando memória")
            return
        try:
            cur = conn.cursor()
            
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
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rodadas_data ON rodadas(data_hora DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_previsoes_acertou ON previsoes(acertou)")
            
            conn.commit()
            cur.close()
            logger.info("✅ Tabelas criadas/verificadas (Agente Z3 v1.0)")
        except Exception as e:
            logger.error(f"Erro tabelas: {e}")
        finally:
            conn.close()
    
    def _init_massive_tables(self):
        """Tabelas adicionais para o Agente em Massa"""
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            
            # Tabela de validações em massa
            cur.execute("""
                CREATE TABLE IF NOT EXISTS validacoes_massa (
                    id SERIAL PRIMARY KEY,
                    batch_id VARCHAR(50) NOT NULL,
                    total_validados INT NOT NULL,
                    acertos INT NOT NULL,
                    erros INT NOT NULL,
                    precisao DECIMAL(5,2) NOT NULL,
                    desvio_detectado BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # Tabela de estatísticas por lote
            cur.execute("""
                CREATE TABLE IF NOT EXISTS estatisticas_lote (
                    id SERIAL PRIMARY KEY,
                    lote_inicio TIMESTAMP NOT NULL,
                    lote_fim TIMESTAMP NOT NULL,
                    total_rodadas INT NOT NULL,
                    total_previsoes INT NOT NULL,
                    precisao_media DECIMAL(5,2),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_validacoes_batch ON validacoes_massa(batch_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_estatisticas_lote ON estatisticas_lote(lote_fim DESC)")
            
            conn.commit()
            cur.close()
            logger.info("✅ Tabelas do Agente em Massa criadas")
        except Exception as e:
            logger.error(f"Erro tabelas massa: {e}")
        finally:
            conn.close()
    
    def salvar_rodada(self, rodada: Rodada) -> bool:
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
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("UPDATE previsoes SET acertou = %s WHERE rodada_id = %s", (acertou, rodada_id))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Erro atualizar acerto: {e}")
        finally:
            conn.close()
    
    def salvar_estado_z3(self, estado: List[int], posicao: int, rounds_usados: int) -> bool:
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("UPDATE estado_z3 SET ativo = FALSE WHERE ativo = TRUE")
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
    
    def salvar_validacao_massa(self, batch_id: str, resultado: ResultadoValidacao) -> bool:
        """Salva resultado de validação em massa"""
        conn = self._get_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO validacoes_massa (batch_id, total_validados, acertos, erros, precisao, desvio_detectado)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (batch_id, resultado.total_validados, resultado.acertos, 
                  resultado.erros, resultado.precisao, resultado.desvio_detectado))
            conn.commit()
            cur.close()
            return True
        except Exception as e:
            logger.error(f"Erro salvar validacao massa: {e}")
            return False
        finally:
            conn.close()
    
    def get_historico_rodadas(self, limit: int = 200) -> List[List[int]]:
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
    
    def get_rodadas_para_validacao(self, limit: int = 50) -> List[Dict]:
        """Busca rodadas com previsões não validadas para validação em massa"""
        conn = self._get_connection()
        if not conn:
            return []
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT r.id, r.p1, r.p2, r.b1, r.b2, r.resultado,
                       p.id as previsao_id, p.p1 as p_p1, p.p2 as p_p2, 
                       p.b1 as p_b1, p.b2 as p_b2
                FROM rodadas r
                JOIN previsoes p ON r.id = p.rodada_id
                WHERE p.acertou IS NULL
                ORDER BY r.data_hora ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            
            resultados = []
            for r in rows:
                resultados.append({
                    'rodada_id': r[0],
                    'real': [r[1], r[2], r[3], r[4]],
                    'resultado_real': r[5],
                    'previsao_id': r[6],
                    'previsto': [r[7], r[8], r[9], r[10]]
                })
            return resultados
        except Exception as e:
            logger.error(f"Erro get_rodadas_validacao: {e}")
            return []
        finally:
            conn.close()
    
    def atualizar_previsoes_em_massa(self, validacoes: List[Tuple[str, bool]]) -> int:
        """Atualiza múltiplas previsões de uma vez"""
        conn = self._get_connection()
        if not conn:
            return 0
        try:
            cur = conn.cursor()
            atualizadas = 0
            for previsao_id, acertou in validacoes:
                cur.execute("UPDATE previsoes SET acertou = %s WHERE id = %s", (acertou, previsao_id))
                atualizadas += 1
            conn.commit()
            cur.close()
            logger.info(f"✅ {atualizadas} previsões atualizadas em massa")
            return atualizadas
        except Exception as e:
            logger.error(f"Erro atualizar previsoes massa: {e}")
            return 0
        finally:
            conn.close()
    
    def get_ultimas_rodadas(self, limit: int = 20) -> List[Dict]:
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
    
    def get_estatisticas_massa(self) -> Dict:
        """Retorna estatísticas das validações em massa"""
        conn = self._get_connection()
        if not conn:
            return {}
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    COUNT(*) as total_batches,
                    AVG(precisao) as precisao_media,
                    SUM(acertos) as total_acertos,
                    SUM(erros) as total_erros,
                    SUM(CASE WHEN desvio_detectado THEN 1 ELSE 0 END) as desvios_detectados
                FROM validacoes_massa
            """)
            row = cur.fetchone()
            cur.close()
            
            return {
                'total_batches': row[0] or 0,
                'precisao_media': round(row[1], 2) if row[1] else 0,
                'total_acertos': row[2] or 0,
                'total_erros': row[3] or 0,
                'desvios_detectados': row[4] or 0
            }
        except Exception as e:
            return {}
        finally:
            conn.close()


# =============================================================================
# AGENTE Z3
# =============================================================================

class AgenteZ3:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.estado_recuperado = None
        self.posicao_atual = 0
        self.estado_original = None
        self.estado_atual = None
        self.previsoes_cache = []
    
    def recuperar_estado_do_banco(self) -> bool:
        return False
    
    def recuperar_estado_dos_dados(self) -> Optional[List[int]]:
        historico = self.db.get_historico_rodadas(ROUNDS_FOR_RECOVERY + 50)
        
        if len(historico) < ROUNDS_FOR_RECOVERY:
            logger.warning(f"[Z3] Histórico insuficiente: {len(historico)} < {ROUNDS_FOR_RECOVERY}")
            return None
        
        logger.info(f"[Z3] Iniciando recuperação com {len(historico)} rodadas...")
        
        bloco = historico[:ROUNDS_FOR_RECOVERY]
        
        outputs = []
        for r in bloco:
            for key in ORDER:
                if key == 'p1':
                    outputs.append(r[0])
                elif key == 'p2':
                    outputs.append(r[1])
                elif key == 'b1':
                    outputs.append(r[2])
                else:
                    outputs.append(r[3])
        
        N = 624
        state = [BitVec(f's_{i}', 32) for i in range(N)]
        s = Then('simplify', 'bit-blast', 'sat').solver()
        s.set("timeout", 300000)
        
        logger.info(f"[Z3] Adicionando {len(outputs)} restrições...")
        
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
            
            self.db.salvar_estado_z3(estado, 0, ROUNDS_FOR_RECOVERY)
            
            return estado
        
        logger.error("[Z3] ✗ Falha na recuperação do estado")
        return None
    
    def _twist(self, state: List[int]) -> List[int]:
        new = list(state)
        for i in range(624):
            y = (new[i] & 0x80000000) + (new[(i+1) % 624] & 0x7FFFFFFF)
            new[i] = new[(i+397) % 624] ^ (y >> 1)
            if y & 1:
                new[i] ^= 0x9908B0DF
        return new
    
    def _temper_py(self, y: int) -> int:
        y ^= (y >> 11)
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= (y >> 18)
        return y & 0xFFFFFFFF
    
    def _mt_to_dice(self, v: int) -> int:
        return ((v * 6) >> 32) + 1
    
    def avancar_estado(self, quantidade_rounds: int = 1):
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
    
    def prever_rodada_especifica(self, quantidade_avancar: int) -> DadosRodada:
        """Preve uma rodada específica após avançar N rodadas"""
        if not self.estado_atual:
            return None
        
        # Salva estado atual
        estado_salvo = list(self.estado_atual)
        pos_salva = self.posicao_atual
        
        # Avança até a rodada desejada
        self.avancar_estado(quantidade_avancar)
        
        # Gera previsão da próxima rodada
        rng = list(self.estado_atual)
        idx = self.posicao_atual
        
        dados = []
        for _ in range(4):
            if idx >= 624:
                rng = self._twist(rng)
                idx = 0
            val = self._temper_py(rng[idx])
            dados.append(self._mt_to_dice(val))
            idx += 1
        
        previsao = DadosRodada(p1=dados[0], p2=dados[1], b1=dados[2], b2=dados[3])
        
        # Restaura estado
        self.estado_atual = estado_salvo
        self.posicao_atual = pos_salva
        
        return previsao
    
    def verificar_rodada(self, dados_reais: DadosRodada, dados_previstos: DadosRodada = None) -> Tuple[bool, DadosRodada]:
        if dados_previstos is None:
            if not self.previsoes_cache:
                return False, None
            if len(self.previsoes_cache) > 0:
                dados_previstos = self.previsoes_cache.pop(0)
            else:
                return False, None
        
        acertou = dados_reais.to_list() == dados_previstos.to_list()
        self.avancar_estado(1)
        
        return acertou, dados_previstos


# =============================================================================
# AGENTE EM MASSA (NOVO!)
# =============================================================================

class AgenteMassivo:
    """
    AGENTE EM MASSA - Processa lotes de rodadas em paralelo
    
    Funcionalidades:
    1. Validação em lote de previsões pendentes
    2. Processamento paralelo com ThreadPoolExecutor
    3. Detecção de desvios no estado
    4. Reconstrução automática do estado se necessário
    5. Estatísticas agregadas por lote
    """
    
    def __init__(self, db: DatabaseManager, agente_z3: AgenteZ3):
        self.db = db
        self.agente_z3 = agente_z3
        self.executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
        self.batch_id = 0
        self.estatisticas_lote = {
            'total_validado': 0,
            'acertos': 0,
            'erros': 0,
            'precisao_atual': 0
        }
    
    def validar_previsoes_pendentes(self) -> ResultadoValidacao:
        """
        Valida todas as previsões pendentes em lote (EM MASSA)
        Processa em paralelo para maior eficiência
        """
        logger.info("[AGENTE MASSA] Iniciando validação em lote...")
        
        pendentes = self.db.get_rodadas_para_validacao(VALIDATION_BATCH)
        
        if not pendentes:
            logger.info("[AGENTE MASSA] Nenhuma previsão pendente para validar")
            return ResultadoValidacao(0, 0, 0, 0, False, "Nenhuma previsão pendente")
        
        logger.info(f"[AGENTE MASSA] Encontradas {len(pendentes)} previsões pendentes")
        
        # Processa em paralelo
        resultados_validacao = []
        
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            futures = []
            for p in pendentes:
                future = executor.submit(self._validar_uma_previsao, p)
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    resultado = future.result(timeout=5)
                    resultados_validacao.append(resultado)
                except Exception as e:
                    logger.error(f"[AGENTE MASSA] Erro na validação paralela: {e}")
        
        # Atualiza banco em massa
        atualizacoes = [(r['previsao_id'], r['acertou']) for r in resultados_validacao]
        self.db.atualizar_previsoes_em_massa(atualizacoes)
        
        # Calcula estatísticas
        total = len(resultados_validacao)
        acertos = sum(1 for r in resultados_validacao if r['acertou'])
        erros = total - acertos
        precisao = (acertos / total * 100) if total > 0 else 0
        
        # Verifica desvio (se precisão caiu muito)
        desvio_detectado = precisao < 50 and total >= 10
        
        resultado = ResultadoValidacao(
            total_validados=total,
            acertos=acertos,
            erros=erros,
            precisao=round(precisao, 2),
            desvio_detectado=desvio_detectado,
            mensagem=f"Validados {total} previsões: {acertos} acertos, {erros} erros ({precisao:.1f}%)"
        )
        
        # Salva no banco
        self.batch_id += 1
        batch_id_str = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.batch_id}"
        self.db.salvar_validacao_massa(batch_id_str, resultado)
        
        # Atualiza estatísticas do agente
        self.estatisticas_lote['total_validado'] += total
        self.estatisticas_lote['acertos'] += acertos
        self.estatisticas_lote['erros'] += erros
        self.estatisticas_lote['precisao_atual'] = (
            self.estatisticas_lote['acertos'] / self.estatisticas_lote['total_validado'] * 100
        ) if self.estatisticas_lote['total_validado'] > 0 else 0
        
        logger.info(f"[AGENTE MASSA] {resultado.mensagem}")
        
        if desvio_detectado:
            logger.warning("[AGENTE MASSA] ⚠️ DESVIO DETECTADO! Precisão baixa. Reconstruindo estado...")
            self._reconstruir_estado()
        
        return resultado
    
    def _validar_uma_previsao(self, pendente: Dict) -> Dict:
        """Valida uma única previsão (usado em paralelo)"""
        real = pendente['real']
        previsto = pendente['previsto']
        acertou = (real == previsto)
        
        return {
            'previsao_id': pendente['previsao_id'],
            'acertou': acertou,
            'rodada_id': pendente['rodada_id']
        }
    
    def _reconstruir_estado(self):
        """Reconstrói o estado do Z3 quando detecta desvio"""
        logger.info("[AGENTE MASSA] Reconstruindo estado do Z3...")
        
        novo_estado = self.agente_z3.recuperar_estado_dos_dados()
        
        if novo_estado:
            logger.info("[AGENTE MASSA] ✅ Estado reconstruído com sucesso!")
            # Limpa cache de previsões antigas
            self.agente_z3.previsoes_cache = []
            # Gera novas previsões
            self.agente_z3.prever_proximas(20)
        else:
            logger.error("[AGENTE MASSA] ❌ Falha na reconstrução do estado!")
    
    def processar_lote_paralelo(self, dados_lote: List[Dict]) -> List[Dict]:
        """
        Processa um lote de dados em paralelo
        Útil para prever múltiplas rodadas futuras simultaneamente
        """
        logger.info(f"[AGENTE MASSA] Processando lote de {len(dados_lote)} itens em paralelo...")
        
        resultados = []
        
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
            futures = []
            for item in dados_lote:
                future = executor.submit(self._processar_item_paralelo, item)
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    resultado = future.result(timeout=10)
                    resultados.append(resultado)
                except Exception as e:
                    logger.error(f"[AGENTE MASSA] Erro no item paralelo: {e}")
        
        logger.info(f"[AGENTE MASSA] Lote processado: {len(resultados)} resultados")
        return resultados
    
    def _processar_item_paralelo(self, item: Dict) -> Dict:
        """Processa um item individual em paralelo"""
        offset = item.get('offset', 0)
        previsao = self.agente_z3.prever_rodada_especifica(offset)
        
        return {
            'offset': offset,
            'previsao': previsao.to_dict() if previsao else None,
            'timestamp': datetime.now().isoformat()
        }
    
    def validar_em_massa_continuo(self):
        """Validação contínua em massa (roda em thread separada)"""
        logger.info("[AGENTE MASSA] Iniciando validação contínua em massa...")
        
        while True:
            try:
                # Valida a cada 30 segundos ou quando acumular 10+ pendentes
                pendentes = self.db.get_rodadas_para_validacao(10)
                
                if len(pendentes) >= 5:
                    self.validar_previsoes_pendentes()
                
                time.sleep(30)  # Espera 30 segundos entre validações em massa
                
            except Exception as e:
                logger.error(f"[AGENTE MASSA] Erro na validação contínua: {e}")
                time.sleep(60)
    
    def get_estatisticas_massa(self) -> Dict:
        """Retorna estatísticas do agente em massa"""
        stats_db = self.db.get_estatisticas_massa()
        
        return {
            'em_massa': {
                'total_validado_agente': self.estatisticas_lote['total_validado'],
                'acertos_agente': self.estatisticas_lote['acertos'],
                'erros_agente': self.estatisticas_lote['erros'],
                'precisao_agente': round(self.estatisticas_lote['precisao_atual'], 2)
            },
            'banco': stats_db,
            'workers_paralelos': PARALLEL_WORKERS,
            'batch_size': BATCH_SIZE,
            'validation_batch': VALIDATION_BATCH
        }


# =============================================================================
# AGENTE PREDITOR NEURAL
# =============================================================================

class AgenteNeural:
    def __init__(self):
        self.historico_acertos = deque(maxlen=100)
        self.ultima_taxa_acerto = 0
        self.alertas = []
    
    def registrar_resultado(self, previsao: DadosRodada, real: DadosRodada, acertou: bool):
        self.historico_acertos.append(1 if acertou else 0)
        
        if len(self.historico_acertos) >= 10:
            taxa = sum(self.historico_acertos) / len(self.historico_acertos) * 100
            self.ultima_taxa_acerto = taxa
            
            if taxa < 50:
                self.alertas.append({
                    'tipo': 'BAIXA_PRECISAO',
                    'mensagem': f'Taxa de acerto abaixo de 50%: {taxa:.1f}%',
                    'timestamp': datetime.now().isoformat()
                })
                logger.warning(f"[NEURAL] Alerta: Baixa precisão ({taxa:.1f}%)")
    
    def get_confianca_ajustada(self, confianca_base: float) -> float:
        if len(self.historico_acertos) < 10:
            return confianca_base
        
        if self.ultima_taxa_acerto < 60:
            return confianca_base * 0.7
        elif self.ultima_taxa_acerto > 80:
            return confianca_base * 1.1
        
        return confianca_base
    
    def get_estatisticas(self) -> Dict:
        if not self.historico_acertos:
            return {'total': 0, 'precisao': 0, 'alertas': []}
        
        return {
            'total': len(self.historico_acertos),
            'precisao': self.ultima_taxa_acerto,
            'alertas': self.alertas[-5:] if self.alertas else []
        }


# =============================================================================
# COLETOR DE API - TRÊS APIS A CADA 0.3s
# =============================================================================

class ColetorAPI:
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
        try:
            rodada_id = dados.get('id') or dados.get('_id')
            
            if not rodada_id:
                return None
            
            if rodada_id in IDS_PROCESSADOS or rodada_id in ULTIMO_ID_CONTROLE:
                return None
            
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
        rodadas_encontradas = []
        
        for nome, url in self.APIS:
            try:
                response = requests.get(url, headers=HEADERS, timeout=3)
                
                if response.status_code == 200:
                    self.api_stats[nome]['sucessos'] += 1
                    self.api_stats[nome]['ultimo_uso'] = datetime.now()
                    
                    dados = response.json()
                    
                    if nome == 'API_DIRETO' and isinstance(dados, list):
                        for item in dados:
                            rodada = self.extrair_dados_rodada(item, nome)
                            if rodada:
                                rodadas_encontradas.append(rodada)
                    else:
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
        rodadas = self.buscar_todas_apis()
        
        for rodada in rodadas:
            if rodada.id == self.ultima_rodada_id:
                continue
            
            self.ultima_rodada_id = rodada.id
            self.rodadas_processadas += 1
            
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
agente_massivo = AgenteMassivo(db, agente_z3)
agente_neural = AgenteNeural()
coletor = ColetorAPI(db)

cache = {
    'ultimas_rodadas': [],
    'ultimas_previsoes': [],
    'estatisticas': {},
    'estado_z3': {'recuperado': False, 'posicao': 0},
    'agente_massa': {}
}


def processar_rodada(rodada: Rodada):
    global cache
    
    if agente_z3.estado_recuperado:
        acertou, previsao_usada = agente_z3.verificar_rodada(rodada.dados)
        
        if previsao_usada:
            # Registra no agente neural também
            agente_neural.registrar_resultado(previsao_usada, rodada.dados, acertou)
            db.atualizar_acerto_previsao(rodada.id, acertou)
            
            if acertou:
                logger.info(f"✅ PREVISÃO CORRETA! {previsao_usada.to_list()} == {rodada.dados.to_list()}")
            else:
                logger.warning(f"❌ PREVISÃO ERRADA! Previsto={previsao_usada.to_list()} Real={rodada.dados.to_list()}")
    
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
    
    while len(cache['ultimas_rodadas']) > 50:
        cache['ultimas_rodadas'].pop()
    
    cache['estatisticas'] = db.get_estatisticas_previsoes()
    cache['estatisticas']['total_rodadas'] = db.get_total_rodadas()
    cache['estatisticas']['api_stats'] = coletor.api_stats
    cache['estatisticas']['agente_neural'] = agente_neural.get_estatisticas()
    cache['agente_massa'] = agente_massivo.get_estatisticas_massa()


def recuperar_estado_inicial():
    logger.info("[INICIALIZAÇÃO] Tentando recuperar estado do Z3...")
    
    estado = agente_z3.recuperar_estado_dos_dados()
    
    if estado:
        cache['estado_z3']['recuperado'] = True
        cache['estado_z3']['posicao'] = agente_z3.posicao_atual
        
        previsoes = agente_z3.prever_proximas(20)
        cache['ultimas_previsoes'] = [p.to_dict() for p in previsoes]
        
        logger.info("[INICIALIZAÇÃO] Estado recuperado! Previsões geradas.")
    else:
        logger.warning("[INICIALIZAÇÃO] Não foi possível recuperar o estado ainda. Aguardando mais dados...")


def loop_coleta():
    logger.info(f"🔄 Loop de coleta iniciado - Intervalo: {UPDATE_INTERVAL}s")
    
    ultima_recuperacao = 0
    ultima_validacao_massa = 0
    
    while True:
        try:
            start_time = time.time()
            
            coletor.coletar_e_processar(agente_z3, processar_rodada)
            
            if not cache['estado_z3']['recuperado']:
                if time.time() - ultima_recuperacao > 30:
                    recuperar_estado_inicial()
                    ultima_recuperacao = time.time()
            
            if cache['estado_z3']['recuperado'] and len(cache['ultimas_previsoes']) < 10:
                novas = agente_z3.prever_proximas(20)
                cache['ultimas_previsoes'] = [p.to_dict() for p in novas]
            
            # Validação em massa a cada 60 segundos
            if time.time() - ultima_validacao_massa > 60:
                resultado = agente_massivo.validar_previsoes_pendentes()
                if resultado.total_validados > 0:
                    cache['agente_massa']['ultima_validacao'] = {
                        'total': resultado.total_validados,
                        'precisao': resultado.precisao,
                        'desvio': resultado.desvio_detectado,
                        'timestamp': datetime.now().isoformat()
                    }
                ultima_validacao_massa = time.time()
            
            elapsed = time.time() - start_time
            sleep_time = max(0, UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_time)
            
        except Exception as e:
            logger.error(f"Erro no loop de coleta: {e}")
            time.sleep(UPDATE_INTERVAL)


def loop_validacao_massa():
    """Thread separada para validação em massa contínua"""
    logger.info("[AGENTE MASSA] Thread de validação em massa iniciada")
    
    while True:
        try:
            agente_massivo.validar_previsoes_pendentes()
            time.sleep(60)
        except Exception as e:
            logger.error(f"[AGENTE MASSA] Erro na thread: {e}")
            time.sleep(120)


# =============================================================================
# ROTAS DA API
# =============================================================================

@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as e:
        return f"""
        <!DOCTYPE html>
        <html>
        <head><title>BAC BO BOT - Agente Z3 + Massa</title></head>
        <body>
            <h1>BAC BO BOT - Agente Z3 v1.0 + Agente em Massa</h1>
            <p>API está funcionando. Use os endpoints abaixo:</p>
            <ul>
                <li><a href="/api/stats">/api/stats</a> - Estatísticas</li>
                <li><a href="/api/rodadas">/api/rodadas</a> - Últimas rodadas</li>
                <li><a href="/api/previsoes">/api/previsoes</a> - Previsões</li>
                <li><a href="/api/apis">/api/apis</a> - Status das APIs</li>
                <li><a href="/api/massa">/api/massa</a> - Estatísticas do Agente em Massa</li>
                <li><a href="/api/validar">/api/validar</a> - Forçar validação em massa</li>
            </ul>
        </body>
        </html>
        """


@app.route('/api/stats')
def api_stats():
    return jsonify({
        'success': True,
        'data': cache['estatisticas'],
        'estado_z3': cache['estado_z3'],
        'agente_massa': cache['agente_massa'],
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/rodadas')
def api_rodadas():
    limit = request.args.get('limit', 30, type=int)
    rodadas = db.get_ultimas_rodadas(limit)
    return jsonify({'success': True, 'data': rodadas})


@app.route('/api/previsoes')
def api_previsoes():
    return jsonify({
        'success': True,
        'data': cache['ultimas_previsoes'],
        'quantidade': len(cache['ultimas_previsoes']),
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/apis')
def api_apis():
    return jsonify({
        'success': True,
        'data': {
            'apis': coletor.api_stats,
            'intervalo': UPDATE_INTERVAL,
            'ultima_atualizacao': datetime.now().isoformat()
        }
    })


@app.route('/api/massa')
def api_massa():
    """Endpoint específico para estatísticas do Agente em Massa"""
    return jsonify({
        'success': True,
        'data': agente_massivo.get_estatisticas_massa(),
        'configuracoes': {
            'batch_size': BATCH_SIZE,
            'parallel_workers': PARALLEL_WORKERS,
            'validation_batch': VALIDATION_BATCH
        },
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/validar')
def api_validar():
    """Força validação em massa imediata"""
    resultado = agente_massivo.validar_previsoes_pendentes()
    return jsonify({
        'success': True,
        'resultado': {
            'total_validados': resultado.total_validados,
            'acertos': resultado.acertos,
            'erros': resultado.erros,
            'precisao': resultado.precisao,
            'desvio_detectado': resultado.desvio_detectado,
            'mensagem': resultado.mensagem
        },
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/recover')
def api_recover():
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
    print("🚀 BAC BO BOT - AGENTE Z3 + MASSA v1.0")
    print("="*70)
    print("\n🎯 ARQUITETURA COMPLETA:")
    print("   1. AGENTE Z3 - Recupera estado do MT19937 (individual)")
    print("   2. AGENTE PREDITOR - Gera previsões determinísticas")
    print("   3. AGENTE VALIDADOR - Verifica previsões contra realidade")
    print("   4. AGENTE EM MASSA - Processa lotes em paralelo (NOVO!)")
    print("   5. AGENTE NEURAL - Ajuste de confiança (complementar)")
    print("\n📡 APIs CONFIGURADAS:")
    print(f"   1. API_DIRETO: {API_DIRETO[:60]}...")
    print(f"   2. API_LATEST: {API_LATEST}")
    print(f"   3. API_BACKUP: {API_BACKUP}")
    print(f"\n⏱️  INTERVALO DE ATUALIZAÇÃO: {UPDATE_INTERVAL} segundos")
    print(f"🎲 ROUNDS PARA RECUPERAÇÃO: {ROUNDS_FOR_RECOVERY}")
    print(f"🔧 CONFIGURAÇÃO Z3: {MAP_TYPE} | ordem={','.join(ORDER)} | offset={OFFSET}")
    print("\n📊 AGENTE EM MASSA:")
    print(f"   - Workers paralelos: {PARALLEL_WORKERS}")
    print(f"   - Batch size: {BATCH_SIZE}")
    print(f"   - Validation batch: {VALIDATION_BATCH}")
    print("\n📊 TABELAS DO BANCO DE DADOS:")
    print("   - rodadas:        Armazena cada rodada coletada")
    print("   - previsoes:      Armazena previsões do Z3")
    print("   - estado_z3:      Armazena estado MT19937 recuperado")
    print("   - validacoes_massa: Armazena validações em lote (NOVO!)")
    print("   - estatisticas_lote: Estatísticas agregadas por lote (NOVO!)")
    print("="*70 + "\n")
    
    # Tenta recuperar estado inicial
    recuperar_estado_inicial()
    
    # Inicia thread de coleta (0.3s)
    coleta_thread = threading.Thread(target=loop_coleta, daemon=True)
    coleta_thread.start()
    
    # Inicia thread de validação em massa (a cada 60s)
    massa_thread = threading.Thread(target=loop_validacao_massa, daemon=True)
    massa_thread.start()
    
    logger.info(f"🌐 Servidor rodando na porta {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
