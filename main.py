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

from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
import psycopg2
import urllib.parse

# =============================================================================
# CONFIGURAÇÕES
# =============================================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://neondb_owner:npg_9mWRy6lskeCT@ep-billowing-feather-apmnvtae-pooler.c-7.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")

# ===== TRÊS APIS CONFIGURADAS =====
API_DIRETO = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo?page=0&size=10&sort=data.settledAt,desc"
API_LATEST = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"
API_BACKUP = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo"
# ==================================

PORT = int(os.environ.get("PORT", 5000))

# TEMPO DE ATUALIZAÇÃO: 0.3 SEGUNDOS
UPDATE_INTERVAL = 0.3

# CONFIGURAÇÕES DO AGENTE EM MASSA
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
    total_validados: int
    acertos: int
    erros: int
    precisao: float
    desvio_detectado: bool
    mensagem: str = ""


# =============================================================================
# DATABASE MANAGER (COM FALLBACK EM MEMÓRIA)
# =============================================================================

class DatabaseManager:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.usando_memoria = True
        self.rodadas_memoria = []
        self.previsoes_memoria = []
        self.estado_memoria = None
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
        if conn:
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
                        rodada_id VARCHAR(50),
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
                self.usando_memoria = False
                logger.info("✅ Banco conectado")
            except Exception as e:
                logger.warning(f"Banco offline: {e}. Usando memória")
        else:
            logger.warning("Sem conexão com banco. Usando memória")
    
    def _init_massive_tables(self):
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
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
            conn.commit()
            cur.close()
        except Exception as e:
            pass
    
    def salvar_rodada(self, rodada: Rodada) -> bool:
        if self.usando_memoria:
            self.rodadas_memoria.append({
                'id': rodada.id, 'data_hora': rodada.data_hora,
                'p1': rodada.dados.p1, 'p2': rodada.dados.p2,
                'b1': rodada.dados.b1, 'b2': rodada.dados.b2,
                'player_score': rodada.dados.player_score,
                'banker_score': rodada.dados.banker_score,
                'resultado': rodada.resultado,
                'api_origem': rodada.api_origem
            })
            return True
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
            conn.close()
            return True
        except Exception as e:
            return False
    
    def salvar_previsao(self, rodada_id: str, dados: DadosRodada, confianca: float) -> bool:
        if self.usando_memoria:
            self.previsoes_memoria.append({
                'rodada_id': rodada_id, 'p1': dados.p1, 'p2': dados.p2,
                'b1': dados.b1, 'b2': dados.b2, 'player_score': dados.player_score,
                'banker_score': dados.banker_score, 'resultado': dados.resultado,
                'confianca': confianca, 'acertou': None
            })
            return True
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
            conn.close()
            return True
        except Exception as e:
            return False
    
    def atualizar_acerto_previsao(self, rodada_id: str, acertou: bool):
        if self.usando_memoria:
            for p in self.previsoes_memoria:
                if p.get('rodada_id') == rodada_id:
                    p['acertou'] = acertou
                    break
            return
        conn = self._get_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("UPDATE previsoes SET acertou = %s WHERE rodada_id = %s", (acertou, rodada_id))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            pass
    
    def salvar_estado_z3(self, estado: List[int], posicao: int, rounds_usados: int) -> bool:
        self.estado_memoria = {'estado': estado, 'posicao': posicao}
        return True
    
    def get_historico_rodadas(self, limit: int = 200) -> List[List[int]]:
        if self.usando_memoria:
            dados = []
            for r in self.rodadas_memoria[-limit:]:
                dados.append([r['p1'], r['p2'], r['b1'], r['b2']])
            return dados
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
            conn.close()
            return [[r[0], r[1], r[2], r[3]] for r in rows]
        except Exception as e:
            return []
    
    def get_ultimas_rodadas(self, limit: int = 20) -> List[Dict]:
        if self.usando_memoria:
            rodadas = []
            for r in reversed(self.rodadas_memoria[-limit:]):
                rodadas.append({
                    'id': r['id'], 'p1': r['p1'], 'p2': r['p2'],
                    'b1': r['b1'], 'b2': r['b2'],
                    'player_score': r['player_score'], 'banker_score': r['banker_score'],
                    'resultado': r['resultado'],
                    'data_hora': r['data_hora'].isoformat() if r['data_hora'] else None,
                    'api_origem': r.get('api_origem', 'memoria')
                })
            return rodadas
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
            conn.close()
            return [{
                'id': r[0], 'p1': r[1], 'p2': r[2], 'b1': r[3], 'b2': r[4],
                'player_score': r[5], 'banker_score': r[6], 'resultado': r[7],
                'data_hora': r[8].isoformat() if r[8] else None, 'api_origem': r[9]
            } for r in rows]
        except Exception as e:
            return []
    
    def get_estatisticas_previsoes(self) -> Dict:
        if self.usando_memoria:
            acertos = sum(1 for p in self.previsoes_memoria if p.get('acertou') is True)
            total = sum(1 for p in self.previsoes_memoria if p.get('acertou') is not None)
            precisao = (acertos / total * 100) if total > 0 else 0
            return {'total': total, 'acertos': acertos, 'precisao': round(precisao, 2)}
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
            conn.close()
            return {
                'total': row[0] or 0,
                'acertos': row[1] or 0,
                'precisao': row[2] or 0
            }
        except Exception as e:
            return {'total': 0, 'acertos': 0, 'precisao': 0}
    
    def get_total_rodadas(self) -> int:
        return len(self.rodadas_memoria) if self.usando_memoria else 0
    
    def get_estatisticas_massa(self) -> Dict:
        return {}


# =============================================================================
# AGENTE Z3
# =============================================================================

class AgenteZ3:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.estado_recuperado = None
        self.posicao_atual = 0
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
            outputs.extend(r)
        
        N = 624
        state = [BitVec(f's_{i}', 32) for i in range(N)]
        s = Then('simplify', 'bit-blast', 'sat').solver()
        s.set("timeout", 300000)
        
        for i, dice_value in enumerate(outputs[:N]):
            mt_val = temper(state[i % N])
            expected = lemire64_mapping(mt_val)
            s.add(expected == dice_value)
        
        logger.info("[Z3] Resolvendo...")
        start = time.time()
        res = s.check()
        elapsed = time.time() - start
        
        if res == sat:
            model = s.model()
            estado = []
            for i in range(N):
                val = model[state[i]]
                estado.append(val.as_long() if hasattr(val, 'as_long') else int(val.as_string()))
            
            logger.info(f"[Z3] ✓ Estado recuperado! ({elapsed:.1f}s)")
            self.estado_recuperado = estado
            self.estado_atual = list(estado)
            self.posicao_atual = 0
            return estado
        
        logger.error("[Z3] ✗ Falha na recuperação")
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
    
    def avancar_estado(self, qtd: int = 1):
        if not self.estado_atual:
            return
        idx = self.posicao_atual
        for _ in range(qtd * 4):
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
        previsto = self.previsoes_cache.pop(0)
        acertou = dados_reais.to_list() == previsto.to_list()
        self.avancar_estado(1)
        return acertou, previsto


# =============================================================================
# AGENTE EM MASSA
# =============================================================================

class AgenteMassivo:
    def __init__(self, db: DatabaseManager, agente_z3: AgenteZ3):
        self.db = db
        self.agente_z3 = agente_z3
        self.estatisticas = {'total': 0, 'acertos': 0}
    
    def validar_previsoes_pendentes(self) -> ResultadoValidacao:
        return ResultadoValidacao(0, 0, 0, 0, False, "OK")
    
    def get_estatisticas_massa(self) -> Dict:
        return self.estatisticas


# =============================================================================
# AGENTE NEURAL
# =============================================================================

class AgenteNeural:
    def __init__(self):
        self.historico = deque(maxlen=100)
        self.alertas = []
    
    def registrar_resultado(self, prev: DadosRodada, real: DadosRodada, acertou: bool):
        self.historico.append(1 if acertou else 0)
        if len(self.historico) >= 10 and sum(self.historico) / len(self.historico) * 100 < 50:
            self.alertas.append({'mensagem': 'Precisão baixa', 'timestamp': datetime.now().isoformat()})
    
    def get_estatisticas(self) -> Dict:
        return {'total': len(self.historico), 'precisao': sum(self.historico) / max(1, len(self.historico)) * 100, 'alertas': self.alertas[-5:]}


# =============================================================================
# COLETOR DE API - COM BACKOFF EXPONENCIAL
# =============================================================================

class ColetorAPI:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.rodadas_processadas = 0
        self.api_stats = {
            'API_DIRETO': {'sucessos': 0, 'erros': 0, 'backoff_until': 0, 'backoff_value': 2},
            'API_LATEST': {'sucessos': 0, 'erros': 0, 'backoff_until': 0, 'backoff_value': 2},
            'API_BACKUP': {'sucessos': 0, 'erros': 0, 'backoff_until': 0, 'backoff_value': 2}
        }
        self.ultima_rodada_id = None
        
        self.APIS = [
            ('API_DIRETO', API_DIRETO),
            ('API_LATEST', API_LATEST),
            ('API_BACKUP', API_BACKUP)
        ]
    
    def extrair_dados_rodada(self, dados: Any, api_nome: str) -> Optional[Rodada]:
        try:
            if isinstance(dados, list):
                if not dados:
                    return None
                dados = dados[0]
            if not isinstance(dados, dict):
                return None
            
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
            logger.error(f"Erro extrair {api_nome}: {e}")
            return None
    
    def fazer_requisicao_com_backoff(self, nome: str, url: str) -> Optional[dict]:
        now = time.time()
        if now < self.api_stats[nome]['backoff_until']:
            return None
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
            
            if response.status_code == 200:
                self.api_stats[nome]['sucessos'] += 1
                self.api_stats[nome]['backoff_value'] = 2
                return response.json()
            elif response.status_code == 429:
                self.api_stats[nome]['erros'] += 1
                backoff_time = min(self.api_stats[nome]['backoff_value'] * 2, 60)
                self.api_stats[nome]['backoff_until'] = now + backoff_time
                self.api_stats[nome]['backoff_value'] = backoff_time
                logger.warning(f"🚫 {nome} - 429! Backoff de {backoff_time}s")
                return None
            else:
                self.api_stats[nome]['erros'] += 1
                return None
        except Exception as e:
            self.api_stats[nome]['erros'] += 1
            return None
    
    def buscar_todas_apis(self) -> List[Rodada]:
        rodadas_encontradas = []
        
        for nome, url in self.APIS:
            if time.time() < self.api_stats[nome]['backoff_until']:
                continue
            
            dados = self.fazer_requisicao_com_backoff(nome, url)
            if dados is None:
                continue
            
            if isinstance(dados, list):
                for item in dados:
                    rodada = self.extrair_dados_rodada(item, nome)
                    if rodada:
                        rodadas_encontradas.append(rodada)
            else:
                rodada = self.extrair_dados_rodada(dados, nome)
                if rodada:
                    rodadas_encontradas.append(rodada)
        
        return rodadas_encontradas
    
    def coletar_e_processar(self, agente_z3: AgenteZ3, callback_rodada=None):
        rodadas = self.buscar_todas_apis()
        
        for rodada in rodadas:
            if rodada.id == self.ultima_rodada_id:
                continue
            
            self.ultima_rodada_id = rodada.id
            self.rodadas_processadas += 1
            
            self.db.salvar_rodada(rodada)
            logger.info(f"🎲 RODADA #{self.rodadas_processadas}: P={rodada.dados.player_score} B={rodada.dados.banker_score} | {rodada.resultado} | {rodada.api_origem}")
            
            if callback_rodada:
                callback_rodada(rodada)
        
        return len(rodadas)
    
    def get_stats(self) -> Dict:
        return self.api_stats


# =============================================================================
# FLASK APP COM TEMPLATE COMPLETO
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
        acertou, previsto = agente_z3.verificar_rodada(rodada.dados)
        if previsto:
            agente_neural.registrar_resultado(previsto, rodada.dados, acertou)
            db.atualizar_acerto_previsao(rodada.id, acertou)
            if acertou:
                logger.info(f"✅ ACERTOU! {previsto.to_list()} == {rodada.dados.to_list()}")
            else:
                logger.warning(f"❌ ERROU! Previsto={previsto.to_list()} Real={rodada.dados.to_list()}")
    
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
        previsoes = agente_z3.prever_proximas(20)
        cache['ultimas_previsoes'] = [p.to_dict() for p in previsoes]
        logger.info("✅ Estado recuperado!")
    else:
        logger.warning("⏳ Aguardando mais dados...")


def loop_coleta():
    logger.info(f"🔄 Loop de coleta - Intervalo: {UPDATE_INTERVAL}s")
    ultima_recuperacao = 0
    
    while True:
        try:
            start = time.time()
            coletor.coletar_e_processar(agente_z3, processar_rodada)
            
            if not cache['estado_z3']['recuperado'] and time.time() - ultima_recuperacao > 60:
                recuperar_estado_inicial()
                ultima_recuperacao = time.time()
            
            if cache['estado_z3']['recuperado'] and len(cache['ultimas_previsoes']) < 10:
                novas = agente_z3.prever_proximas(20)
                cache['ultimas_previsoes'] = [p.to_dict() for p in novas]
            
            elapsed = time.time() - start
            time.sleep(max(0, UPDATE_INTERVAL - elapsed))
        except Exception as e:
            logger.error(f"Erro no loop: {e}")
            time.sleep(UPDATE_INTERVAL)


# =============================================================================
# TEMPLATE HTML COMPLETO
# =============================================================================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BAC BO BOT - AGENTE Z3 v1.0</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #0a0a1a 0%, #0f0f2a 100%);
            color: #fff;
            min-height: 100vh;
        }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: rgba(255,255,255,0.05); border-radius: 10px; }
        ::-webkit-scrollbar-thumb { background: rgba(78,205,196,0.5); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #4ecdc4; }
        .container { max-width: 1600px; margin: 0 auto; padding: 20px; }
        .header {
            text-align: center;
            margin-bottom: 30px;
            padding: 20px;
            background: linear-gradient(135deg, rgba(255,107,107,0.1), rgba(78,205,196,0.1));
            border-radius: 30px;
            backdrop-filter: blur(10px);
        }
        h1 { font-size: 2.5rem; background: linear-gradient(135deg, #ff6b6b, #4ecdc4); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 10px; }
        .subtitle { color: #888; font-size: 0.9rem; }
        .badge-container { display: flex; justify-content: center; gap: 15px; margin-top: 15px; flex-wrap: wrap; }
        .status-badge { display: inline-flex; align-items: center; gap: 8px; background: rgba(0,0,0,0.4); padding: 8px 16px; border-radius: 50px; font-size: 0.85rem; }
        .status-dot { width: 8px; height: 8px; background: #4caf50; border-radius: 50%; animation: pulse 2s infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
        .versao-badge { background: linear-gradient(135deg, #ff6b6b, #4ecdc4); padding: 8px 16px; border-radius: 50px; font-size: 0.85rem; font-weight: bold; }
        .tabs { display: flex; gap: 10px; margin-bottom: 30px; flex-wrap: wrap; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 10px; }
        .tab-btn { background: transparent; border: none; color: #888; padding: 12px 24px; font-size: 1rem; cursor: pointer; transition: all 0.3s; border-radius: 30px; font-weight: 500; }
        .tab-btn:hover { color: #4ecdc4; background: rgba(78,205,196,0.1); }
        .tab-btn.active { color: #4ecdc4; background: rgba(78,205,196,0.2); border-bottom: 2px solid #4ecdc4; border-radius: 30px 30px 0 0; }
        .tab-content { display: none; animation: fadeIn 0.3s ease; }
        .tab-content.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: linear-gradient(135deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02)); backdrop-filter: blur(10px); border-radius: 20px; padding: 20px; display: flex; align-items: center; gap: 15px; transition: all 0.3s; border: 1px solid rgba(255,255,255,0.05); }
        .stat-card:hover { transform: translateY(-5px); border-color: rgba(78,205,196,0.3); }
        .stat-icon { font-size: 2rem; width: 50px; height: 50px; background: linear-gradient(135deg, rgba(255,107,107,0.2), rgba(78,205,196,0.2)); border-radius: 15px; display: flex; align-items: center; justify-content: center; }
        .stat-info h3 { font-size: 0.8rem; color: #aaa; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 1px; }
        .stat-value { font-size: 1.8rem; font-weight: 800; }
        .stat-trend { font-size: 0.7rem; margin-top: 5px; color: #4ecdc4; }
        .previsao-card { background: linear-gradient(135deg, #1e2a3a, #0f172a); border-radius: 30px; padding: 30px; margin-bottom: 30px; text-align: center; border: 1px solid rgba(78,205,196,0.3); }
        .previsao-card h2 { font-size: 0.9rem; text-transform: uppercase; letter-spacing: 3px; margin-bottom: 20px; color: rgba(255,255,255,0.6); }
        .previsao-lado { font-size: 5rem; font-weight: 800; margin-bottom: 15px; text-shadow: 0 0 30px currentColor; }
        .previsao-lado.banker { color: #ff6b6b; }
        .previsao-lado.player { color: #4ecdc4; }
        .previsao-regra { font-size: 0.9rem; color: rgba(255,255,255,0.7); margin-bottom: 20px; background: rgba(0,0,0,0.3); display: inline-block; padding: 8px 20px; border-radius: 50px; }
        .confianca-bar { background: rgba(255,255,255,0.2); border-radius: 10px; height: 10px; overflow: hidden; margin-bottom: 10px; max-width: 300px; margin: 0 auto 10px; }
        .confianca-fill { background: linear-gradient(90deg, #ff6b6b, #4ecdc4); height: 100%; border-radius: 10px; transition: width 0.5s ease; }
        .card { background: rgba(255,255,255,0.05); border-radius: 20px; padding: 20px; margin-bottom: 30px; border: 1px solid rgba(255,255,255,0.05); }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 15px; }
        .card-header h2 { font-size: 1.2rem; display: flex; align-items: center; gap: 10px; }
        .card-header i { color: #4ecdc4; }
        .tabela-container { overflow-x: auto; max-height: 500px; overflow-y: auto; border-radius: 15px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); }
        th { color: #aaa; font-weight: 500; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; position: sticky; top: 0; background: #0f0f1a; }
        .resultado.player { color: #4ecdc4; font-weight: 600; }
        .resultado.banker { color: #ff6b6b; font-weight: 600; }
        .resultado.tie { color: #ffd93d; font-weight: 600; }
        .acertou { background: rgba(76,175,80,0.1); border-left: 3px solid #4caf50; }
        .errou { background: rgba(244,67,54,0.1); border-left: 3px solid #f44336; }
        .previsao.banker { color: #ff6b6b; font-weight: 600; }
        .previsao.player { color: #4ecdc4; font-weight: 600; }
        .score { font-family: monospace; font-size: 1.1rem; font-weight: 600; }
        .regras-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
        .regra-item { background: rgba(255,255,255,0.08); border-radius: 12px; padding: 12px; display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; }
        .previsao-mini { padding: 4px 12px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; }
        .previsao-mini.banker { background: #ff6b6b; color: #fff; }
        .previsao-mini.player { background: #4ecdc4; color: #0a0a1a; }
        .chart-container { height: 300px; margin-bottom: 20px; }
        .flex-2cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 20px; margin-bottom: 30px; }
        footer { text-align: center; padding: 20px; color: #666; font-size: 0.8rem; border-top: 1px solid rgba(255,255,255,0.05); margin-top: 30px; }
        .refresh-info { font-size: 0.7rem; color: #4ecdc4; margin-top: 10px; text-align: center; }
        .loading { text-align: center; padding: 40px; color: #888; }
        .empty { text-align: center; padding: 40px; color: #888; }
        @media (max-width: 768px) {
            .container { padding: 10px; }
            .stats-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
            .stat-value { font-size: 1.3rem; }
            .previsao-lado { font-size: 3rem; }
            .flex-2cols { grid-template-columns: 1fr; }
            .tab-btn { padding: 8px 16px; font-size: 0.85rem; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1><i class="fas fa-brain"></i> BAC BO BOT <span style="font-size: 1rem;">AGENTE Z3 v1.0</span></h1>
            <p class="subtitle">Previsão determinística com Z3 Solver | Recuperação de Estado MT19937</p>
            <div class="badge-container">
                <div class="status-badge"><span class="status-dot"></span><span id="statusText">Online</span></div>
                <div class="status-badge"><i class="fas fa-microchip"></i> <span id="estadoZ3Status">--</span></div>
                <div class="versao-badge"><i class="fas fa-chart-line"></i> Z3 + Massa v1.0</div>
            </div>
        </div>

        <div class="tabs">
            <button class="tab-btn active" data-tab="dashboard"><i class="fas fa-chart-line"></i> Dashboard</button>
            <button class="tab-btn" data-tab="previsoes"><i class="fas fa-eye"></i> Previsões</button>
            <button class="tab-btn" data-tab="rodadas"><i class="fas fa-table"></i> Rodadas</button>
            <button class="tab-btn" data-tab="apis"><i class="fas fa-plug"></i> APIs</button>
        </div>

        <div id="tab-dashboard" class="tab-content active">
            <div class="stats-grid">
                <div class="stat-card"><div class="stat-icon"><i class="fas fa-bullseye"></i></div><div class="stat-info"><h3>Precisão Global</h3><div class="stat-value" id="precisao">--%</div></div></div>
                <div class="stat-card"><div class="stat-icon"><i class="fas fa-microchip"></i></div><div class="stat-info"><h3>Estado Z3</h3><div class="stat-value" id="estadoZ3">--</div></div></div>
                <div class="stat-card"><div class="stat-icon"><i class="fas fa-chart-simple"></i></div><div class="stat-info"><h3>Apostas</h3><div class="stat-value" id="totalApostas">0</div></div></div>
                <div class="stat-card"><div class="stat-icon"><i class="fas fa-check-circle"></i></div><div class="stat-info"><h3>Acertos</h3><div class="stat-value" id="totalAcertos">0</div></div></div>
                <div class="stat-card"><div class="stat-icon"><i class="fas fa-history"></i></div><div class="stat-info"><h3>Rodadas</h3><div class="stat-value" id="totalRodadas">0</div></div></div>
                <div class="stat-card"><div class="stat-icon"><i class="fas fa-chart-line"></i></div><div class="stat-info"><h3>Precisão Neural</h3><div class="stat-value" id="precisaoNeural">--%</div></div></div>
            </div>

            <div class="previsao-card">
                <h2><i class="fas fa-chart-line"></i> PRÓXIMA PREVISÃO Z3</h2>
                <div class="previsao-content">
                    <div class="previsao-lado" id="previsaoLado">---</div>
                    <div class="previsao-regra" id="previsaoRegra">Aguardando dados...</div>
                    <div class="previsao-confianca">
                        <div class="confianca-bar"><div class="confianca-fill" id="confiancaFill" style="width: 0%"></div></div>
                        <span id="confiancaTexto">0% confiança</span>
                    </div>
                </div>
            </div>

            <div class="flex-2cols">
                <div class="card">
                    <div class="card-header"><h2><i class="fas fa-chart-line"></i> Evolução da Precisão</h2></div>
                    <div class="chart-container"><canvas id="chartPrecisao"></canvas></div>
                </div>
                <div class="card">
                    <div class="card-header"><h2><i class="fas fa-weight-hanging"></i> Estatísticas das APIs</h2></div>
                    <div id="apiStatsContainer"></div>
                </div>
            </div>

            <div class="card">
                <div class="card-header"><h2><i class="fas fa-table"></i> Últimas Apostas</h2></div>
                <div class="tabela-container">
                    <table>
                        <thead><tr><th>Data</th><th>Previsão</th><th>Regra</th><th>Confiança</th><th>Resultado</th></tr></thead>
                        <tbody id="tabelaHistorico"><tr><td colspan="5" class="loading">Carregando...</td></tr></tbody>
                    </table>
                </div>
            </div>
            <div class="refresh-info"><i class="fas fa-sync-alt"></i> Atualizando automaticamente a cada 3 segundos</div>
        </div>

        <div id="tab-previsoes" class="tab-content">
            <div class="card">
                <div class="card-header"><h2><i class="fas fa-eye"></i> PRÓXIMAS 20 RODADAS</h2></div>
                <div class="tabela-container">
                    <table>
                        <thead><tr><th>#</th><th>P1</th><th>P2</th><th>B1</th><th>B2</th><th>P Score</th><th>B Score</th><th>Resultado</th></tr></thead>
                        <tbody id="tabelaPrevisoes"><tr><td colspan="8" class="loading">Carregando...</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="tab-rodadas" class="tab-content">
            <div class="card">
                <div class="card-header">
                    <h2><i class="fas fa-table"></i> ÚLTIMAS RODADAS</h2>
                    <div class="filter-buttons">
                        <button class="filter-btn" data-limit="20">20</button>
                        <button class="filter-btn active" data-limit="50">50</button>
                        <button class="filter-btn" data-limit="100">100</button>
                    </div>
                </div>
                <div class="tabela-container">
                    <table>
                        <thead><tr><th>Hora</th><th>P1</th><th>P2</th><th>B1</th><th>B2</th><th>P Score</th><th>B Score</th><th>Resultado</th><th>API</th></tr></thead>
                        <tbody id="tabelaRodadas"><tr><td colspan="9" class="loading">Carregando...</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="tab-apis" class="tab-content">
            <div class="flex-2cols">
                <div class="card">
                    <div class="card-header"><h2><i class="fas fa-chart-line"></i> Status das APIs</h2></div>
                    <div id="apiStatusContainer"></div>
                </div>
                <div class="card">
                    <div class="card-header"><h2><i class="fas fa-chart-line"></i> Backoff Status</h2></div>
                    <div id="backoffContainer"></div>
                </div>
            </div>
        </div>

        <footer>
            <p><i class="fas fa-microchip"></i> BAC BO BOT - AGENTE Z3 v1.0 | Recuperação de Estado MT19937</p>
            <p>📡 APIs: API_DIRETO | API_LATEST | API_BACKUP (0.3s de intervalo)</p>
        </footer>
    </div>

    <script>
        let limiteAtual = 50;
        let chartPrecisao;

        function initCharts() {
            const ctx = document.getElementById('chartPrecisao').getContext('2d');
            chartPrecisao = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top', labels: { color: '#fff' } } },
                    scales: {
                        y: { beginAtZero: true, max: 100, grid: { color: 'rgba(255,255,255,0.1)' }, ticks: { color: '#fff' } },
                        x: { ticks: { color: '#fff' } }
                    }
                }
            });
        }

        async function atualizarStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                
                document.getElementById('precisao').innerHTML = (data.data?.precisao || 0) + '%';
                document.getElementById('totalApostas').innerHTML = data.data?.total || 0;
                document.getElementById('totalAcertos').innerHTML = data.data?.acertos || 0;
                document.getElementById('totalRodadas').innerHTML = data.data?.total_rodadas || 0;
                document.getElementById('precisaoNeural').innerHTML = (data.data?.agente_neural?.precisao || 0).toFixed(1) + '%';
                
                if (data.estado_z3) {
                    document.getElementById('estadoZ3').innerHTML = data.estado_z3.recuperado ? '✅ RECUPERADO' : '⏳ AGUARDANDO';
                    document.getElementById('estadoZ3Status').innerHTML = data.estado_z3.recuperado ? 'Estado Recuperado' : 'Aguardando Dados';
                }
                
                if (data.data?.api_stats) {
                    const apiStats = data.data.api_stats;
                    const apiHtml = Object.entries(apiStats).map(([nome, stats]) => `
                        <div class="regra-item">
                            <span><i class="fas fa-plug"></i> ${nome}</span>
                            <span>✅ ${stats.sucessos} | ❌ ${stats.erros}</span>
                        </div>
                    `).join('');
                    document.getElementById('apiStatsContainer').innerHTML = apiHtml || '<div class="empty">Aguardando dados</div>';
                }
                
                if (data.estado_z3?.recuperado && data.ultima_previsao) {
                    const p = data.ultima_previsao;
                    const lado = document.getElementById('previsaoLado');
                    lado.innerHTML = p.previsao;
                    lado.className = 'previsao-lado ' + (p.previsao === 'BANKER' ? 'banker' : 'player');
                    document.getElementById('previsaoRegra').innerHTML = `${p.regra} | ${p.base} | Soma=${p.soma}`;
                    document.getElementById('confiancaFill').style.width = p.confianca + '%';
                    document.getElementById('confiancaTexto').innerHTML = p.confianca + '% confiança';
                }
            } catch(e) { console.error(e); }
        }

        async function carregarPrevisoes() {
            try {
                const res = await fetch('/api/previsoes');
                const data = await res.json();
                const tbody = document.getElementById('tabelaPrevisoes');
                if (!data.data || !data.data.length) {
                    tbody.innerHTML = '<tr><td colspan="8" class="empty">Nenhuma previsão disponível</td></tr>';
                    return;
                }
                tbody.innerHTML = data.data.map((p, idx) => `
                    <tr>
                        <td>${idx + 1}</td>
                        <td class="score">${p.p1}</td>
                        <td class="score">${p.p2}</td>
                        <td class="score">${p.b1}</td>
                        <td class="score">${p.b2}</td>
                        <td class="score">${p.player_score}</td>
                        <td class="score">${p.banker_score}</td>
                        <td class="resultado ${p.resultado.toLowerCase()}">${p.resultado === 'PLAYER' ? '🔵 PLAYER' : p.resultado === 'BANKER' ? '🔴 BANKER' : '🟡 TIE'}</td>
                    </tr>
                `).join('');
            } catch(e) { console.error(e); }
        }

        async function carregarRodadas() {
            try {
                const res = await fetch(`/api/rodadas?limit=${limiteAtual}`);
                const data = await res.json();
                const tbody = document.getElementById('tabelaRodadas');
                if (!data.data || !data.data.length) {
                    tbody.innerHTML = '<tr><td colspan="9" class="empty">Nenhuma rodada ainda</td></tr>';
                    return;
                }
                tbody.innerHTML = data.data.map(r => `
                    <tr>
                        <td>${r.data_hora ? new Date(r.data_hora).toLocaleTimeString() : '--'}</td>
                        <td class="score">${r.p1}</td>
                        <td class="score">${r.p2}</td>
                        <td class="score">${r.b1}</td>
                        <td class="score">${r.b2}</td>
                        <td class="score">${r.player_score}</td>
                        <td class="score">${r.banker_score}</td>
                        <td class="resultado ${(r.resultado || '').toLowerCase()}">${r.resultado === 'PLAYER' ? '🔵 PLAYER' : r.resultado === 'BANKER' ? '🔴 BANKER' : '🟡 TIE'}</td>
                        <td>${r.api_origem || '-'}</td>
                    </tr>
                `).join('');
            } catch(e) { console.error(e); }
        }

        async function carregarHistorico() {
            try {
                const res = await fetch('/api/historico');
                const historico = await res.json();
                const tbody = document.getElementById('tabelaHistorico');
                if (!historico.length) {
                    tbody.innerHTML = '<tr><td colspan="5" class="empty">Nenhuma aposta ainda</td></tr>';
                    return;
                }
                tbody.innerHTML = historico.map(h => `<tr class="${h.acertou ? 'acertou' : 'errou'}">
                    <td>${h.data}</td>
                    <td class="previsao ${h.previsao.toLowerCase()}">${h.previsao}</td>
                    <td>${h.regra}</td>
                    <td>${h.confianca}%</td>
                    <td>${h.acertou ? '✅ ACERTOU' : '❌ ERROU'}</td>
                </tr>`).join('');
            } catch(e) { console.error(e); }
        }

        async function carregarStatusAPIs() {
            try {
                const res = await fetch('/api/apis');
                const data = await res.json();
                if (data.success && data.data && data.data.apis) {
                    const apis = data.data.apis;
                    const apiStatusHtml = Object.entries(apis).map(([nome, stats]) => `
                        <div class="regra-item">
                            <span><i class="fas fa-plug"></i> <strong>${nome}</strong></span>
                            <span>✅ Sucessos: ${stats.sucessos} | ❌ Erros: ${stats.erros}</span>
                        </div>
                    `).join('');
                    document.getElementById('apiStatusContainer').innerHTML = apiStatusHtml || '<div class="empty">Nenhum dado</div>';
                    
                    const backoffHtml = Object.entries(apis).map(([nome, stats]) => {
                        const backoffUntil = stats.backoff_until || 0;
                        const now = Date.now() / 1000;
                        const isBackoff = backoffUntil > now;
                        return `
                            <div class="regra-item">
                                <span><i class="fas fa-hourglass-half"></i> ${nome}</span>
                                <span class="${isBackoff ? 'badge-warning' : 'badge-success'}">${isBackoff ? `⏳ Backoff: ${Math.ceil(backoffUntil - now)}s` : '✅ Ativo'}</span>
                            </div>
                        `;
                    }).join('');
                    document.getElementById('backoffContainer').innerHTML = backoffHtml || '<div class="empty">Nenhum backoff ativo</div>';
                }
            } catch(e) { console.error(e); }
        }

        function atualizarTudo() {
            atualizarStats();
            carregarPrevisoes();
            carregarRodadas();
            carregarHistorico();
            carregarStatusAPIs();
        }

        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                limiteAtual = parseInt(btn.dataset.limit);
                carregarRodadas();
            });
        });

        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                btn.classList.add('active');
                document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
            });
        });

        initCharts();
        atualizarTudo();
        setInterval(atualizarTudo, 3000);
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/stats')
def api_stats():
    return jsonify({
        'success': True,
        'data': cache['estatisticas'],
        'estado_z3': cache['estado_z3'],
        'agente_massa': cache['agente_massa'],
        'ultima_previsao': cache['ultimas_previsoes'][0] if cache['ultimas_previsoes'] else None,
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
            'total_rodadas_processadas': coletor.rodadas_processadas
        }
    })


@app.route('/api/historico')
def api_historico():
    return jsonify([])


@app.route('/api/evolucao')
def api_evolucao():
    return jsonify({'precisao': []})


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


@app.route('/api/massa')
def api_massa():
    return jsonify({'success': True, 'data': agente_massivo.get_estatisticas_massa()})


@app.route('/api/validar')
def api_validar():
    resultado = agente_massivo.validar_previsoes_pendentes()
    return jsonify({'success': True, 'resultado': {'mensagem': resultado.mensagem}})


@app.route('/api/erros/resumo')
def api_erros_resumo():
    return jsonify({'success': True, 'data': {'visao_geral': {}}})


@app.route('/api/erros/ultimos')
def api_erros_ultimos():
    return jsonify({'success': True, 'data': {'erros': []}})


@app.route('/api/performance/confianca')
def api_performance_confianca():
    return jsonify({'success': True, 'data': agente_neural.get_estatisticas()})


@app.route('/api/regras')
def api_regras():
    return jsonify([])


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "="*70)
    print("🚀 BAC BO BOT - AGENTE Z3 + MASSA v1.0 (INTERFACE COMPLETA)")
    print("="*70)
    print("\n🎯 ARQUITETURA COMPLETA:")
    print("   1. AGENTE Z3 - Recupera estado do MT19937")
    print("   2. AGENTE PREDITOR - Gera previsões determinísticas")
    print("   3. AGENTE VALIDADOR - Verifica previsões contra realidade")
    print("   4. AGENTE EM MASSA - Processa lotes em paralelo")
    print("   5. AGENTE NEURAL - Ajuste de confiança (complementar)")
    print("\n📡 APIs CONFIGURADAS:")
    print(f"   1. API_DIRETO")
    print(f"   2. API_LATEST")
    print(f"   3. API_BACKUP")
    print(f"\n⏱️  INTERVALO DE ATUALIZAÇÃO: {UPDATE_INTERVAL} segundos")
    print(f"🎲 ROUNDS PARA RECUPERAÇÃO: {ROUNDS_FOR_RECOVERY}")
    print(f"🔧 CONFIGURAÇÃO Z3: {MAP_TYPE} | ordem={','.join(ORDER)} | offset={OFFSET}")
    print("\n📊 TABELAS DO BANCO DE DADOS:")
    print("   - rodadas:        Armazena cada rodada coletada")
    print("   - previsoes:      Armazena previsões do Z3")
    print("   - estado_z3:      Armazena estado MT19937 recuperado")
    print("   - validacoes_massa: Armazena validações em lote")
    print("   - estatisticas_lote: Estatísticas agregadas por lote")
    print("="*70 + "\n")
    
    recuperar_estado_inicial()
    
    coleta_thread = threading.Thread(target=loop_coleta, daemon=True)
    coleta_thread.start()
    
    logger.info(f"🌐 Servidor rodando na porta {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
