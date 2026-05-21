"""
BAC BO BOT - AGENTE Z3 + NEURAL PREDICTOR v1.0
SISTEMA COM FALLBACK DE API

LÓGICA DE APIs:
1. API_DIRETO (principal) - tenta sempre primeiro
2. API_LATEST (fallback) - só usada se API_DIRETO falhar (timeout/429/erro)
3. NÃO usa API_BACKUP para evitar duplicação de rodadas

APIs:
- API_DIRETO: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc
- API_LATEST: https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest
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
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

# Importações do Z3
import subprocess
import sys

try:
    import z3
    from z3 import *
except ImportError:
    print("[*] Instalando z3-solver...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "z3-solver", "--force-reinstall", "-q"])
    from z3 import *

from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
import urllib.parse

# =============================================================================
# CONFIGURAÇÕES
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_9mWRy6lskeCT@ep-billowing-feather-apmnvtae-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

# APIS - APENAS DUAS (DIRETO principal, LATEST fallback)
API_DIRETO = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc"
API_LATEST = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"
# API_BACKUP REMOVIDA - causa duplicação de rodadas

PORT = int(os.environ.get("PORT", 5000))
UPDATE_INTERVAL = 0.3

BATCH_SIZE = 50
PARALLEL_WORKERS = 4
VALIDATION_BATCH = 20

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive'
}

# =============================================================================
# CONFIGURAÇÕES DO Z3
# =============================================================================

ORDER = ["p1", "p2", "b1", "b2"]
OFFSET = 0
MAP_TYPE = "Lemire64"
ROUNDS_FOR_RECOVERY = 156

# Cache de IDs já processados
IDS_PROCESSADOS = set()

# =============================================================================
# FUNÇÕES Z3
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
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rodadas_id ON rodadas(id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rodadas_data ON rodadas(data_hora DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_previsoes_acertou ON previsoes(acertou)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_previsoes_rodada_id ON previsoes(rodada_id)")
            
            conn.commit()
            cur.close()
            logger.info("✅ Tabelas criadas/verificadas")
        except Exception as e:
            logger.error(f"Erro tabelas: {e}")
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
            return True
        except Exception as e:
            logger.error(f"Erro salvar estado: {e}")
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
                       COALESCE(ROUND(AVG(CASE WHEN acertou THEN 100 ELSE 0 END), 2), 0) as precisao
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
    
    def verificar_rodada(self, dados_reais: DadosRodada) -> Tuple[bool, Optional[DadosRodada]]:
        if not self.previsoes_cache:
            return False, None
        
        dados_previstos = self.previsoes_cache.pop(0)
        acertou = dados_reais.to_list() == dados_previstos.to_list()
        self.avancar_estado(1)
        
        return acertou, dados_previstos


# =============================================================================
# AGENTE EM MASSA
# =============================================================================

class AgenteMassivo:
    def __init__(self, db: DatabaseManager, agente_z3: AgenteZ3):
        self.db = db
        self.agente_z3 = agente_z3
        self.estatisticas_lote = {'total_validado': 0, 'acertos': 0, 'erros': 0, 'precisao_atual': 0}
    
    def validar_previsoes_pendentes(self) -> ResultadoValidacao:
        logger.info("[AGENTE MASSA] Iniciando validação...")
        
        pendentes = self.db.get_rodadas_para_validacao(VALIDATION_BATCH)
        
        if not pendentes:
            return ResultadoValidacao(0, 0, 0, 0, False, "Nenhuma previsão pendente")
        
        resultados_validacao = []
        for p in pendentes:
            real = p['real']
            previsto = p['previsto']
            acertou = (real == previsto)
            resultados_validacao.append({
                'previsao_id': p['previsao_id'],
                'acertou': acertou
            })
        
        atualizacoes = [(r['previsao_id'], r['acertou']) for r in resultados_validacao]
        self.db.atualizar_previsoes_em_massa(atualizacoes)
        
        total = len(resultados_validacao)
        acertos = sum(1 for r in resultados_validacao if r['acertou'])
        erros = total - acertos
        precisao = (acertos / total * 100) if total > 0 else 0
        desvio_detectado = precisao < 50 and total >= 10
        
        self.estatisticas_lote['total_validado'] += total
        self.estatisticas_lote['acertos'] += acertos
        self.estatisticas_lote['erros'] += erros
        self.estatisticas_lote['precisao_atual'] = (
            self.estatisticas_lote['acertos'] / self.estatisticas_lote['total_validado'] * 100
        ) if self.estatisticas_lote['total_validado'] > 0 else 0
        
        logger.info(f"[AGENTE MASSA] Validados {total}: {acertos} acertos, {erros} erros ({precisao:.1f}%)")
        
        return ResultadoValidacao(total, acertos, erros, precisao, desvio_detectado, "")
    
    def get_estatisticas_massa(self) -> Dict:
        return {
            'total_validado': self.estatisticas_lote['total_validado'],
            'acertos': self.estatisticas_lote['acertos'],
            'erros': self.estatisticas_lote['erros'],
            'precisao': round(self.estatisticas_lote['precisao_atual'], 2)
        }


# =============================================================================
# AGENTE NEURAL
# =============================================================================

class AgenteNeural:
    def __init__(self):
        self.historico_acertos = deque(maxlen=100)
        self.ultima_taxa_acerto = 0
    
    def registrar_resultado(self, acertou: bool):
        self.historico_acertos.append(1 if acertou else 0)
        
        if len(self.historico_acertos) >= 10:
            taxa = sum(self.historico_acertos) / len(self.historico_acertos) * 100
            self.ultima_taxa_acerto = taxa
            if taxa < 50:
                logger.warning(f"[NEURAL] Alerta: Baixa precisão ({taxa:.1f}%)")
    
    def get_estatisticas(self) -> Dict:
        if not self.historico_acertos:
            return {'total': 0, 'precisao': 0}
        return {
            'total': len(self.historico_acertos),
            'precisao': round(self.ultima_taxa_acerto, 2)
        }


# =============================================================================
# COLETOR DE API - COM FALLBACK (SEM BACKUP)
# =============================================================================

class ColetorAPI:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.rodadas_processadas = 0
        self.api_stats = {
            'API_DIRETO': {'sucessos': 0, 'erros': 0, 'ultimo_id': None},
            'API_LATEST': {'sucessos': 0, 'erros': 0, 'ultimo_id': None}
        }
        self.fallback_em_uso = False
        self.ultimo_erro_direto = 0
    
    def _extrair_rodada(self, dados: dict, api_nome: str) -> Optional[Rodada]:
        try:
            rodada_id = dados.get('id') or dados.get('_id')
            if not rodada_id:
                return None
            
            if rodada_id in IDS_PROCESSADOS:
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
            
            if not all(isinstance(v, int) and 1 <= v <= 6 for v in [p1, p2, b1, b2]):
                return None
            
            dados_rodada = DadosRodada(p1=p1, p2=p2, b1=b1, b2=b2)
            resultado = dados.get('resultado') or dados_rodada.resultado
            
            IDS_PROCESSADOS.add(rodada_id)
            
            return Rodada(
                id=rodada_id,
                data_hora=datetime.now(),
                dados=dados_rodada,
                resultado=resultado,
                fonte=api_nome,
                api_origem=api_nome
            )
        except Exception as e:
            logger.error(f"Erro extrair rodada {api_nome}: {e}")
            return None
    
    def _requisitar_api(self, nome: str, url: str) -> Optional[Any]:
        try:
            response = requests.get(url, headers=HEADERS, timeout=8)
            
            if response.status_code == 200:
                self.api_stats[nome]['sucessos'] += 1
                return response.json()
            else:
                self.api_stats[nome]['erros'] += 1
                logger.warning(f"⚠️ {nome} retornou {response.status_code}")
                return None
        except requests.Timeout:
            self.api_stats[nome]['erros'] += 1
            logger.warning(f"⏰ Timeout na {nome}")
            return None
        except Exception as e:
            self.api_stats[nome]['erros'] += 1
            logger.warning(f"❌ Erro na {nome}: {e}")
            return None
    
    def coletar_e_processar(self, callback_rodada=None) -> int:
        """
        Lógica de coleta:
        1. Tenta API_DIRETO primeiro
        2. Se falhar (erro/timeout/429), usa API_LATEST como fallback
        3. NUNCA usa API_BACKUP para evitar duplicação
        """
        rodadas_encontradas = []
        
        # TENTA API_DIRETO PRIMEIRO
        logger.info("📡 Tentando API_DIRETO...")
        dados_direto = self._requisitar_api('API_DIRETO', API_DIRETO)
        
        if dados_direto is not None:
            # API_DIRETO funcionou!
            self.fallback_em_uso = False
            self.ultimo_erro_direto = 0
            
            # Processa resposta da API_DIRETO (pode ser lista ou dict)
            items = dados_direto if isinstance(dados_direto, list) else [dados_direto]
            for item in items:
                rodada = self._extrair_rodada(item, 'API_DIRETO')
                if rodada:
                    rodadas_encontradas.append(rodada)
            
            if rodadas_encontradas:
                logger.info(f"✅ API_DIRETO: {len(rodadas_encontradas)} rodada(s) nova(s)")
        else:
            # API_DIRETO FALHOU! Usa fallback API_LATEST
            logger.warning("⚠️ API_DIRETO falhou! Usando API_LATEST como fallback...")
            self.fallback_em_uso = True
            self.ultimo_erro_direto = time.time()
            
            dados_latest = self._requisitar_api('API_LATEST', API_LATEST)
            
            if dados_latest is not None:
                # Processa resposta da API_LATEST
                items = dados_latest if isinstance(dados_latest, list) else [dados_latest]
                for item in items:
                    rodada = self._extrair_rodada(item, 'API_LATEST')
                    if rodada:
                        rodadas_encontradas.append(rodada)
                
                if rodadas_encontradas:
                    logger.info(f"🔄 API_LATEST (fallback): {len(rodadas_encontradas)} rodada(s) nova(s)")
                else:
                    logger.warning("⚠️ API_LATEST não retornou rodadas novas")
            else:
                logger.error("❌ Ambas APIs falharam! Nenhuma rodada coletada.")
        
        # Processa as rodadas encontradas
        for rodada in rodadas_encontradas:
            self.rodadas_processadas += 1
            self.db.salvar_rodada(rodada)
            logger.info(f"🎲 RODADA #{self.rodadas_processadas}: {rodada.resultado} | {rodada.api_origem}")
            
            if callback_rodada:
                callback_rodada(rodada)
        
        return len(rodadas_encontradas)
    
    def get_stats(self) -> Dict:
        return {
            'API_DIRETO': self.api_stats['API_DIRETO'],
            'API_LATEST': self.api_stats['API_LATEST'],
            'fallback_em_uso': self.fallback_em_uso,
            'total_rodadas': self.rodadas_processadas
        }


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
            agente_neural.registrar_resultado(acertou)
            db.atualizar_acerto_previsao(rodada.id, acertou)
            
            if acertou:
                logger.info(f"✅ PREVISÃO CORRETA!")
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
    cache['estatisticas']['api_stats'] = coletor.get_stats()
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
        logger.warning("[INICIALIZAÇÃO] Aguardando mais dados para recuperar estado...")


def loop_coleta():
    logger.info(f"🔄 Loop de coleta iniciado - Intervalo: {UPDATE_INTERVAL}s")
    
    ultima_recuperacao = 0
    ultima_validacao = 0
    
    while True:
        try:
            start_time = time.time()
            
            coletor.coletar_e_processar(processar_rodada)
            
            if not cache['estado_z3']['recuperado']:
                if time.time() - ultima_recuperacao > 30:
                    recuperar_estado_inicial()
                    ultima_recuperacao = time.time()
            
            if cache['estado_z3']['recuperado'] and len(cache['ultimas_previsoes']) < 10:
                novas = agente_z3.prever_proximas(20)
                cache['ultimas_previsoes'] = [p.to_dict() for p in novas]
            
            if time.time() - ultima_validacao > 60:
                resultado = agente_massivo.validar_previsoes_pendentes()
                if resultado.total_validados > 0:
                    cache['agente_massa']['ultima_validacao'] = {
                        'total': resultado.total_validados,
                        'precisao': resultado.precisao,
                        'timestamp': datetime.now().isoformat()
                    }
                ultima_validacao = time.time()
            
            elapsed = time.time() - start_time
            sleep_time = max(0, UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_time)
            
        except Exception as e:
            logger.error(f"Erro no loop de coleta: {e}")
            time.sleep(UPDATE_INTERVAL)


# =============================================================================
# ROTAS DA API
# =============================================================================

@app.route('/')
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>BAC BO BOT - Agente Z3</title></head>
    <body style="font-family: Arial; background: #0a0a1a; color: #fff; text-align: center; padding: 50px;">
        <h1>🚀 BAC BO BOT - AGENTE Z3</h1>
        <p>Sistema de previsão determinística com Z3 Solver</p>
        <div style="display: flex; justify-content: center; gap: 20px; margin-top: 30px; flex-wrap: wrap;">
            <a href="/api/stats" style="background: #4ecdc4; color: #0a0a1a; padding: 10px 20px; border-radius: 30px;">📊 Estatísticas</a>
            <a href="/api/rodadas" style="background: #4ecdc4; color: #0a0a1a; padding: 10px 20px; border-radius: 30px;">🎲 Rodadas</a>
            <a href="/api/previsoes" style="background: #4ecdc4; color: #0a0a1a; padding: 10px 20px; border-radius: 30px;">🔮 Previsões</a>
            <a href="/api/apis" style="background: #4ecdc4; color: #0a0a1a; padding: 10px 20px; border-radius: 30px;">📡 APIs</a>
        </div>
        <div style="margin-top: 40px; font-size: 12px; color: #666;">
            <p>📡 APIs: DIRETO (principal) | LATEST (fallback)</p>
            <p>⏱️ Intervalo: 0.3s | Z3: Recuperação de Estado MT19937</p>
        </div>
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
    stats = coletor.get_stats()
    return jsonify({
        'success': True,
        'data': {
            'apis': stats,
            'intervalo': UPDATE_INTERVAL,
            'total_rodadas_processadas': coletor.rodadas_processadas,
            'ultima_atualizacao': datetime.now().isoformat()
        }
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


@app.route('/api/validar')
def api_validar():
    resultado = agente_massivo.validar_previsoes_pendentes()
    return jsonify({
        'success': True,
        'resultado': {
            'total_validados': resultado.total_validados,
            'acertos': resultado.acertos,
            'erros': resultado.erros,
            'precisao': resultado.precisao,
            'desvio_detectado': resultado.desvio_detectado
        }
    })


def main():
    print("\n" + "="*70)
    print("🚀 BAC BO BOT - AGENTE Z3 v1.0 (FALLBACK CORRETO)")
    print("="*70)
    print("\n📡 LÓGICA DE APIs:")
    print("   1. API_DIRETO (principal) - tenta sempre primeiro")
    print("   2. API_LATEST (fallback) - só se DIRETO falhar")
    print("   3. API_BACKUP - REMOVIDA (causava duplicação)")
    print(f"\n⏱️  INTERVALO: {UPDATE_INTERVAL}s")
    print(f"🎲 ROUNDS PARA RECUPERAÇÃO: {ROUNDS_FOR_RECOVERY}")
    print("="*70 + "\n")
    
    recuperar_estado_inicial()
    
    coleta_thread = threading.Thread(target=loop_coleta, daemon=True)
    coleta_thread.start()
    
    logger.info(f"🌐 Servidor rodando na porta {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
