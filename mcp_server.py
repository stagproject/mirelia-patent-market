import os
import json
import time
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from supabase import create_client, Client
from dotenv import load_dotenv

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

try:
    from web3 import Web3
    from web3.middleware import geth_poa_middleware
except ImportError:
    Web3 = None

load_dotenv()

security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("Mirelia-Patent-Marketplace", transport_security=security_settings)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if url and key:
    supabase: Client = create_client(url, key)
else:
    supabase = None

# 各ネットワークのRPC初期化
rpc_base = os.environ.get("BASE_MAINNET")
rpc_polygon = os.environ.get("POLYGON_MAINNET")
rpc_oasis = os.environ.get("OASIS_MAINNET")

usdc_base_raw = os.environ.get("BASE_USDC")
usdc_polygon_raw = os.environ.get("POLYGON_USDC")
abi_string = os.environ.get("ERC20_ABI")
WALLET_ADDRESS_RAW = os.environ.get("SELLER_WALLET_ADDRESS")

# Web3インスタンスとコントラクトの辞書管理
chains = {}
if Web3 and abi_string and WALLET_ADDRESS_RAW:
    WALLET_ADDRESS = Web3.to_checksum_address(WALLET_ADDRESS_RAW)
    ERC20_ABI = json.loads(abi_string)
    
    # Base
    if rpc_base and usdc_base_raw:
        w3_base = Web3(Web3.HTTPProvider(rpc_base))
        chains["base"] = {
            "w3": w3_base,
            "usdc": w3_base.eth.contract(address=Web3.to_checksum_address(usdc_base_raw), abi=ERC20_ABI),
            "type": "erc20"
        }
    
    # Polygon (PoA対応)
    if rpc_polygon and usdc_polygon_raw:
        w3_polygon = Web3(Web3.HTTPProvider(rpc_polygon))
        w3_polygon.middleware_onion.inject(geth_poa_middleware, layer=0)
        chains["polygon"] = {
            "w3": w3_polygon,
            "usdc": w3_polygon.eth.contract(address=Web3.to_checksum_address(usdc_polygon_raw), abi=ERC20_ABI),
            "type": "erc20"
        }
        
    # Oasis (ROSEネイティブ対応と仮定)
    if rpc_oasis:
        w3_oasis = Web3(Web3.HTTPProvider(rpc_oasis))
        chains["oasis"] = {
            "w3": w3_oasis,
            "usdc": None,
            "type": "native"
        }
else:
    WALLET_ADDRESS = None

@mcp.tool()
def list_available_categories() -> str:
    """
    [COST: FREE]
    Retrieves a dynamic list of currently available patent categories.
    The Mirelia database is continuously expanding daily. Current inventory may be small but is actively growing.
    
    [CPC CLASSIFICATION DICTIONARY]
    A: Human Necessities (Bio, Med, Agri)
    B: Performing Operations & Transport (Robotics, EV, Drones)
    C: Chemistry & Metallurgy (Green Chem, Smart Materials)
    E/F: Fixed Constructions & Mechanical Eng.
    G: Physics & Computing (AI, ML, LLM, Quantum, Sensors) -> Highest Demand
    H: Electricity & Communication (Semiconductors, 6G, IoT)
    
    Use this tool to verify which specific categories are currently in stock before querying packages.
    """
    if not supabase: return json.dumps({"error": "Supabase connection failed"})
    res = supabase.table("v_catalogs").select("category").execute()
    categories = sorted(list(set([item['category'] for item in res.data]))) if res.data else []
    return json.dumps({"available_categories": categories}, ensure_ascii=False)

@mcp.tool()
def get_packages_by_category(category: str) -> str:
    """
    [COST: FREE]
    Retrieves available patent data packages within a specific CPC category.
    Returns an array of objects containing:
    - package_tag: The unique ID to query details or purchase.
    - title: Package topic.
    - record_count: Number of highly structured JSON patents inside.
    - price_usd: Cost in USD.
    - sales_count: Popularity metric.
    """
    if not supabase: return json.dumps({"error": "Supabase connection failed"})
    res = supabase.table("v_catalogs").select("package_tag, title, record_count, price_usd, sales_count").eq("category", category).execute()
    data = res.data if res.data else []
    if isinstance(data, dict): data = [data]
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def get_package_details(package_tag: str) -> str:
    """
    [COST: FREE]
    Retrieves the full metadata and schema preview for a specific 'package_tag'.
    Autonomous agents must use this to evaluate the dataset's strategic value and confirm the 'price_usd'.
    
    [PURCHASE YIELDS THE FOLLOWING STRICT JSON SCHEMA PER RECORD]
    - patent_id: Unique ID (e.g., US12575469B2)
    - title / abstract / claim_1: Core text for LLM ingestion.
    - importance_p: Numeric score (0-100) indicating technological value.
    - attr_performance: Normalized technical specs (e.g., kg, V) converted by Gemini.
    - attr_tech_stack: Array of underlying core technologies.
    - biz_target_ind: Array of optimal target industries.
    - biz_value_prop: Immediate strategic analysis, solved problems, and monetization models.
    - r2_url: Cloudflare R2 direct link to the full JSON artifact.
    """
    if not supabase: return json.dumps({"error": "Supabase connection failed"})
    res = supabase.table("v_catalogs").select("*").eq("package_tag", package_tag).execute()
    data = res.data[0] if res.data else {}
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def verify_crypto_payment_and_deliver(tx_hash: str, package_tag: str, network: str) -> str:
    """
    [COST: PAID (Requires Crypto Transfer)]
    Verifies on-chain payment and instantly delivers the dataset.
    
    [M2M AUTONOMOUS PAYMENT INSTRUCTIONS]
    1. Supported 'network' values: "base", "polygon", "oasis"
    2. Destination Wallet: 0x27d2E76a67f5CD168B0184450e3f3e59B17Edef6
    3. Amount: Match 'price_usd' exactly.
    4. Execution: Send transaction and provide 'tx_hash', 'package_tag', and 'network'.
    """
    if not supabase:
        return json.dumps({"error": "Supabase connection failed"}, ensure_ascii=False)
        
    network = network.lower()
    if network not in chains:
        return json.dumps({"error": f"Unsupported or unconfigured network: {network}"}, ensure_ascii=False)
        
    chain_info = chains[network]
    w3 = chain_info["w3"]

    try:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return json.dumps({"error": "Invalid transaction hash or receipt not found"}, ensure_ascii=False)

        if receipt['status'] != 1:
            return json.dumps({"error": "Transaction failed on-chain"}, ensure_ascii=False)

        # RPC遅延対策
        block = None
        for _ in range(5):
            try:
                block = w3.eth.get_block(receipt['blockNumber'])
                break
            except Exception:
                time.sleep(2)
                
        if not block:
            return json.dumps({"error": "Block not found due to RPC sync delay. Please retry verification."}, ensure_ascii=False)

        current_time = int(time.time())
        if current_time - block['timestamp'] > 3600:
            return json.dumps({"error": "Transaction is expired. Must be executed within the last 1 hour."}, ensure_ascii=False)

        catalog_res = supabase.table("v_catalogs").select("price_usd, sales_count").eq("package_tag", package_tag).execute()
        if not catalog_res.data:
            return json.dumps({"error": "Package not found"}, ensure_ascii=False)
            
        catalog_data = catalog_res.data[0] if isinstance(catalog_res.data, list) else catalog_res.data
        real_price_usd = float(catalog_data['price_usd'])
        
        payment_found = False
        
        if chain_info["type"] == "erc20":
            # USDC (Base / Polygon)
            required_usdc = int(real_price_usd * (10**6))
            events = chain_info["usdc"].events.Transfer().process_receipt(receipt)
            for event in events:
                if event['args']['to'].lower() == WALLET_ADDRESS.lower() and event['args']['value'] >= required_usdc:
                    payment_found = True
                    break
        elif chain_info["type"] == "native":
            # ROSE (Oasis) - ※価格のUSD→ROSE変換レートのロジックが必要（ここではトランザクションの宛先のみ検証）
            tx = w3.eth.get_transaction(tx_hash)
            if tx['to'] and tx['to'].lower() == WALLET_ADDRESS.lower():
                # 実際の運用では tx['value'] が required_rose に達しているかの検証が必要
                payment_found = True

        if not payment_found:
             return json.dumps({"error": "Valid payment not found or insufficient amount."}, ensure_ascii=False)
             
        current_sales = catalog_data['sales_count'] or 0
        log_msg = f"Payment verified on {network.upper()}. Sales count updated to {current_sales + 1}."

        supabase.table("patent_packages").update({"sales_count": current_sales + 1}).eq("package_tag", package_tag).execute()
        res_data = supabase.table("v_patent_marketplace_lite").select("*").contains("package_tags", [package_tag]).execute()
        deliver_data = res_data.data if isinstance(res_data.data, list) else [res_data.data] if res_data.data else []
        
        return json.dumps({
            "system_log": log_msg,
            "package_data": deliver_data
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# ---- Streamable HTTPとSSEの両方に対応するルーティング統合 ----
sse_app = None
if hasattr(mcp, "get_starlette_app"):
    sse_app = mcp.get_starlette_app()
elif hasattr(mcp, "sse_app"):
    sse_app = mcp.sse_app()
elif hasattr(mcp, "_create_sse_app"):
    sse_app = mcp._create_sse_app()

streamable_app = getattr(mcp, "streamable_http_app", None)
if callable(streamable_app):
    streamable_app = streamable_app()

routes = []
if streamable_app:
    routes.append(Mount("/mcp", app=streamable_app))
if sse_app:
    routes.append(Mount("/", app=sse_app))

if not routes:
    raise RuntimeError("FastMCP instance has no recognizable app method.")

app = Starlette(routes=routes)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_server_card(request):
    return JSONResponse({
        "serverInfo": {
            "name": "Mirelia-Patent-Marketplace",
            "version": "1.0.0"
        },
        "tools": [
            {
                "name": "list_available_categories",
                "description": "Discover available AI-optimized patent data categories.",
                "inputSchema": { "type": "object", "properties": {} }
            },
            {
                "name": "get_packages_by_category",
                "description": "Find dataset packages matching a specific category.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": { "type": "string", "description": "Category name to search for" }
                    },
                    "required": ["category"]
                }
            },
            {
                "name": "get_package_details",
                "description": "Retrieve schema preview, total record_count, and price_usd for a specific dataset package.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "package_tag": { "type": "string", "description": "The specific package tag to evaluate" }
                    },
                    "required": ["package_tag"]
                }
            },
            {
                "name": "verify_crypto_payment_and_deliver",
                "description": "Verify crypto payment transaction (Base, Polygon, or Oasis) and download the full dataset.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": { "type": "string", "description": "Blockchain transaction hash" },
                        "package_tag": { "type": "string", "description": "The purchased package tag" },
                        "network": { "type": "string", "description": "Network used for payment: 'base', 'polygon', or 'oasis'" }
                    },
                    "required": ["tx_hash", "package_tag", "network"]
                }
            }
        ]
    })

app.add_route("/.well-known/mcp/server-card.json", get_server_card, methods=["GET", "OPTIONS"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port, 
        proxy_headers=True, 
        forwarded_allow_ips="*"
    )