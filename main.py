#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FreeMcServer 自动续订脚本
- 支持多账号（环境变量 FREEMCSERVER，每行格式：邮箱-----密码）
- 自动处理 Cloudflare 整页挑战（登录页面）
- 自动处理 AdBlocker 检测弹窗和整页拦截
- 直接访问续订页面并处理 Turnstile
- 每一步截图，通过 Telegram 通知结果
"""

import os
import sys
import time
import re
import platform
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ================== 配置 ==================
BASE_URL = "https://panel.freemcserver.net"
LOGIN_URL = f"{BASE_URL}/user/login"
SERVER_INDEX_URL = f"{BASE_URL}/server/index"

OUTPUT_DIR = Path("output/screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("freemcserver-keepalive")

# ================== 辅助函数 ==================
def is_linux() -> bool:
    return platform.system().lower() == "linux"

def mask_email(email: str) -> str:
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        first_char = domain[0]
        masked_domain = f"{first_char}***.{tld}" if len(parts) > 1 else f"{first_char}***"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"

def mask_server_id(server_id: str) -> str:
    """隐藏服务器 ID 中间部分（只用于日志）"""
    if len(server_id) <= 4:
        return server_id
    return server_id[:2] + "***" + server_id[-1]

def mask_server_name(server_name: str, server_id: str) -> str:
    """隐藏服务器名称中的 ID（只用于日志）"""
    if server_id in server_name:
        return server_name.replace(server_id, mask_server_id(server_id))
    return server_name

def mask_url(url: str) -> str:
    return re.sub(r'/server/\d+', '/server/***', url)

def setup_display():
    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            logger.info("虚拟显示已启动")
            return display
        except Exception as e:
            logger.error(f"虚拟显示启动失败: {e}")
            sys.exit(1)
    return None

def screenshot_path(account_index: int, name: str) -> str:
    return str(OUTPUT_DIR / f"{datetime.now().strftime('%H%M%S')}-acc{account_index}-{name}.png")

def safe_screenshot(sb, path: str, result: Optional[Dict] = None):
    try:
        sb.save_screenshot(path)
        logger.info(f"📸 截图 → {Path(path).name}")
        if result is not None:
            result.setdefault("screenshots", []).append(path)
    except Exception as e:
        logger.warning(f"截图失败: {e}")

def notify_telegram(account_index: int, email: str, server_results: List[Dict], overall_success: bool, overall_message: str = "", screenshot_file: str = None):
    """
    发送 Telegram 通知
    
    Args:
        account_index: 账号索引
        email: 邮箱（不隐藏，用于 TG 通知）
        server_results: 服务器续订结果列表 [{"id": "xxx", "name": "xxx", "success": True, "before": "...", "after": "..."}]
        overall_success: 总体是否成功
        overall_message: 总体消息
        screenshot_file: 截图文件路径
    """
    try:
        token = os.environ.get("TG_BOT_TOKEN")
        chat_id = os.environ.get("TG_CHAT_ID")
        if not token or not chat_id:
            return

        status = "✅ 续订成功" if overall_success else "❌ 续订失败"
        text = f"{status}\n\n"
        text += f"账号：{email}\n"
        
        # 如果有服务器结果，显示详细信息
        if server_results:
            for sr in server_results:
                server_id = sr.get("id", "未知")
                before = sr.get("before", "")
                after = sr.get("after", "")
                
                text += f"服务器：{server_id}\n"
                
                if before and after:
                    text += f"到期: {before} -> {after}\n"
                elif after:
                    text += f"到期: {after}\n"
                elif before:
                    text += f"到期: {before}\n"
        
        text += f"\nFreeMcServer Auto Renew"

        if screenshot_file and Path(screenshot_file).exists():
            with open(screenshot_file, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": f},
                    timeout=60
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=30
            )
    except Exception as e:
        logger.warning(f"Telegram 通知失败: {e}")

def parse_accounts() -> List[Tuple[str, str]]:
    raw = os.environ.get("FREEMCSERVER", "")
    accounts = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "-----" in line:
            parts = line.split("-----", 1)
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                accounts.append((email, password))
                logger.info(f"发现账号: {mask_email(email)}")
            else:
                logger.warning(f"账号格式错误（邮箱或密码为空）: {line}")
        else:
            logger.warning(f"账号格式错误（缺少 -----）: {line}")
    return accounts

# ================== AdBlocker 绕过脚本注入 ==================
def inject_adblock_bypass(sb):
    """
    在页面加载早期注入 JavaScript，绕过 AdBlocker 检测
    """
    try:
        sb.execute_script('''
            // 1. 禁用 AdBlocker 检测脚本
            (function() {
                // 阻止 AdBlocker 检测库加载
                var origSetAttribute = Element.prototype.setAttribute;
                Element.prototype.setAttribute = function(name, value) {
                    if (name === 'src' && value) {
                        var src = value.toString().toLowerCase();
                        if (src.includes('adblock') || src.includes('detect-adblocker') ||
                            src.includes('fuckadblock') || src.includes('blockadblock')) {
                            return;
                        }
                    }
                    return origSetAttribute.call(this, name, value);
                };
                
                // 禁用常见的检测方法
                window.blockAdBlock = undefined;
                window.fuckAdBlock = undefined;
                window.detectedAdblocker = false;
                
                // 覆盖 getComputedStyle 以隐藏 display:none 元素
                var origGetComputedStyle = window.getComputedStyle;
                window.getComputedStyle = function(el, pseudo) {
                    var styles = origGetComputedStyle.call(window, el, pseudo);
                    if (el && el.className && el.className.includes('adblock')) {
                        return origGetComputedStyle.call(window, document.body, pseudo);
                    }
                    return styles;
                };
                
                // 覆盖 fetch 以拦截广告检测请求
                if (window.fetch) {
                    var origFetch = window.fetch;
                    window.fetch = function(...args) {
                        var url = args[0] ? args[0].toString().toLowerCase() : '';
                        if (url.includes('ads.') || url.includes('adserver') || 
                            url.includes('doubleclick') || url.includes('adblocker-detect') ||
                            url.includes('/ads/') || url.includes('advertisement')) {
                            return Promise.resolve(new Response(JSON.stringify({}), {status: 200}));
                        }
                        return origFetch.apply(this, args);
                    };
                }
                
                // 覆盖 XMLHttpRequest
                if (window.XMLHttpRequest) {
                    var origOpen = XMLHttpRequest.prototype.open;
                    XMLHttpRequest.prototype.open = function(method, url) {
                        if (url && url.toString().toLowerCase().includes('adblocker-detect')) {
                            this._blocked = true;
                            return;
                        }
                        return origOpen.apply(this, arguments);
                    };
                    
                    var origSend = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.send = function(data) {
                        if (this._blocked) {
                            this.onreadystatechange && this.onreadystatechange();
                            return;
                        }
                        return origSend.apply(this, arguments);
                    };
                }
            })();
        ''')
        logger.info("✅ 已注入 AdBlocker 检测绕过脚本")
    except Exception as e:
        logger.warning(f"注入绕过脚本失败: {e}")

# ================== AdBlocker 整页检测和处理 ==================
def remove_adblocker_page(sb) -> bool:
    """
    直接移除 AdBlocker 警告页面
    """
    try:
        removed = sb.execute_script('''
            (function() {
                var title = document.title || '';
                var bodyText = document.body ? document.body.innerText : '';
                
                // 检查是否是 AdBlocker 页面
                if (title.includes('Turn off your adblocker') || 
                    title.includes('adblocker') ||
                    bodyText.includes('Please turn off your adblocker') ||
                    bodyText.includes('I have disabled my AdBlocker')) {
                    
                    logger.info("检测到 AdBlocker 页面，执行移除...");
                    
                    // 移除整个 AdBlocker 容器
                    var adblockDiv = document.querySelector('.site-adblock');
                    if (adblockDiv) {
                        adblockDiv.remove();
                    }
                    
                    // 隐藏 wrapper
                    var wrapper = document.querySelector('section#wrapper');
                    if (wrapper) {
                        wrapper.style.display = 'none';
                    }
                    
                    // 隐藏背景图
                    var particlesJs = document.querySelector('#particles-js');
                    if (particlesJs) {
                        particlesJs.style.display = 'none';
                    }
                    
                    // 清除 body 背景
                    document.body.style.background = 'none';
                    document.body.innerHTML = '<div style="display:none">Bypassed</div>';
                    
                    return true;
                }
                return false;
            })();
        ''')
        
        if removed:
            logger.info("✅ 已移除 AdBlocker 警告页面")
            time.sleep(2)
            
            # 重定向到服务器列表
            logger.info("执行重定向...")
            sb.open(SERVER_INDEX_URL)
            time.sleep(3)
            return True
        return False
    except Exception as e:
        logger.warning(f"移除 AdBlocker 页面异常: {e}")
        return False

def check_and_handle_adblocker_page(sb, account_index: int, result: Dict) -> bool:
    """
    检测并处理 AdBlocker 整页警告
    返回: True 表示可以继续，False 表示被拦截且无法绕过
    """
    try:
        # 检测是否是 AdBlocker 整页
        is_adblock_page = sb.execute_script('''
            (function() {
                var title = document.title || '';
                var bodyText = document.body ? document.body.innerText : '';
                
                // 检查标题
                if (title.includes('Turn off your adblocker') || 
                    title.includes('adblocker')) return true;
                
                // 检查页面内容
                if (bodyText.includes('Please turn off your adblocker') ||
                    bodyText.includes('I have disabled my AdBlocker')) return true;
                
                // 检查特定元素
                if (document.querySelector('.site-adblock')) return true;
                
                return false;
            })();
        ''')
        
        if not is_adblock_page:
            return True
        
        logger.warning("🚨 检测到 AdBlocker 整页警告")
        safe_screenshot(sb, screenshot_path(account_index, "adblocker-page"), result)
        
        # 方法1：直接移除页面并重定向
        if remove_adblocker_page(sb):
            time.sleep(2)
            safe_screenshot(sb, screenshot_path(account_index, "adblocker-page-removed"), result)
            return True
        
        # 方法2：点击按钮绕过
        logger.info("尝试点击 AdBlocker 确认按钮...")
        clicked = sb.execute_script('''
            (function() {
                var links = document.querySelectorAll('a, button');
                for (var i = 0; i < links.length; i++) {
                    var text = links[i].innerText || '';
                    if (text.includes('disabled my AdBlocker') || 
                        text.includes('I have disabled') ||
                        text.includes('I have disabled my AdBlocker')) {
                        links[i].click();
                        return true;
                    }
                }
                return false;
            })();
        ''')
        
        if clicked:
            logger.info("已点击 AdBlocker 确认按钮，等待跳转...")
            time.sleep(5)
            safe_screenshot(sb, screenshot_path(account_index, "adblocker-page-button-clicked"), result)
            
            # 检查是否还在 AdBlocker 页面
            still_blocked = sb.execute_script('''
                (function() {
                    var title = document.title || '';
                    return title.includes('Turn off your adblocker');
                })();
            ''')
            
            if still_blocked:
                logger.warning("AdBlocker 警告未解除，尝试强制重定向")
                sb.open(SERVER_INDEX_URL)
                time.sleep(3)
                return True
            else:
                logger.info("✅ AdBlocker 警告已绕过")
                return True
        else:
            logger.error("未找到 AdBlocker 确认按钮")
            return False
            
    except Exception as e:
        logger.warning(f"检测 AdBlocker 整页异常: {e}")
        return True

# ================== AdBlocker 弹窗检测处理 ==================
def handle_adblocker_popup(sb, account_index: int, result: Dict) -> bool:
    try:
        # 检测是否存在 AdBlocker 弹窗（模态框）
        has_modal = sb.execute_script('''
            (function() {
                var modal = document.querySelector('.modal.show, .modal.fade.show');
                if (!modal) return false;
                var text = modal.innerText || '';
                return text.includes('AdBlocker') || text.includes('disable my AdBlocker');
            })();
        ''')
        if not has_modal:
            return True

        logger.info("检测到 AdBlocker 弹窗，尝试关闭...")
        safe_screenshot(sb, screenshot_path(account_index, "adblocker-modal"), result)

        # 尝试点击确认按钮
        clicked = sb.execute_script('''
            (function() {
                var btn = document.querySelector('.modal.show button, .modal.fade.show button');
                if (btn && btn.innerText.includes('disabled my AdBlocker')) {
                    btn.click();
                    return true;
                }
                return false;
            })();
        ''')
        if not clicked:
            # 强制移除弹窗
            sb.execute_script('''
                (function() {
                    var modal = document.querySelector('.modal.show, .modal.fade.show');
                    if (modal) modal.remove();
                    var backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) backdrop.remove();
                    document.body.classList.remove('modal-open');
                })();
            ''')
        time.sleep(2)
        safe_screenshot(sb, screenshot_path(account_index, "adblocker-modal-handled"), result)
        return True
    except Exception as e:
        logger.warning(f"处理 AdBlocker 弹窗异常: {e}")
        return True

# ================== Cloudflare 整页挑战 ==================
def is_cloudflare_interstitial(sb) -> bool:
    try:
        has_login_form = sb.execute_script('''
            return !!(document.querySelector('#loginformmodel-username')
                   || document.querySelector('form[action*="/user/login"]'));
        ''')
        if has_login_form:
            return False
        has_dashboard = sb.execute_script('''
            return !!(document.querySelector('.server-card')
                   || document.querySelector('.server-renew'));
        ''')
        if has_dashboard:
            return False
        page_source = sb.get_page_source()
        title = sb.get_title().lower() if sb.get_title() else ""
        indicators = ["Just a moment", "Verify you are human", "Checking your browser", "Checking if the site connection is secure"]
        for ind in indicators:
            if ind in page_source:
                return True
        if "just a moment" in title or "attention required" in title:
            return True
        body_len = sb.execute_script('return document.body ? document.body.innerText.length : 0;')
        if body_len < 200 and "challenges.cloudflare.com" in page_source:
            return True
        return False
    except:
        return False

def bypass_cloudflare_interstitial(sb, max_attempts=3) -> bool:
    logger.info("检测到 Cloudflare 整页挑战，尝试绕过...")
    for attempt in range(max_attempts):
        logger.info(f"CF 绕过尝试 {attempt+1}/{max_attempts}")
        try:
            sb.uc_gui_click_captcha()
            time.sleep(6)
            if not is_cloudflare_interstitial(sb):
                logger.info("✅ Cloudflare 挑战已通过")
                return True
        except Exception as e:
            logger.warning(f"CF 绕过失败: {e}")
        time.sleep(3)
    logger.info("尝试刷新页面重试...")
    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
        time.sleep(5)
        if not is_cloudflare_interstitial(sb):
            return True
    except:
        pass
    return False

# ================== 获取服务器到期时间 ==================
def get_server_expiry(sb, server_id: str) -> Optional[str]:
    """
    获取服务器到期时间
    
    Returns:
        到期时间字符串，格式：YYYY-MM-DD HH:MM:SS
    """
    try:
        manage_url = f"{BASE_URL}/server/{server_id}"
        sb.open(manage_url)
        time.sleep(3)
        
        # 从页面提取到期时间
        expiry = sb.execute_script('''
            (function() {
                // 方法1: 从 JavaScript 变量读取
                if (window.fmcs && window.fmcs.server_expires_at) {
                    return window.fmcs.server_expires_at;
                }
                
                // 方法2: 从页面文本读取
                var badges = document.querySelectorAll('.badge');
                for (var i = 0; i < badges.length; i++) {
                    var text = badges[i].innerText || '';
                    if (text.includes('Server Expires on:')) {
                        var match = text.match(/(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/);
                        if (match) return match[1];
                    }
                }
                
                return null;
            })();
        ''')
        
        return expiry if expiry else None
    except Exception as e:
        logger.warning(f"获取服务器 {mask_server_id(server_id)} 到期时间失败: {e}")
        return None

# ================== Turnstile 处理 ==================
def handle_turnstile_verification(sb, account_index: int, result: Dict, server_id: str = "") -> bool:
    """
    处理 Turnstile 验证 - 结合自动等待和手动触发
    """
    logger.info("处理 Turnstile 验证...")
    
    # 先滚动到 Turnstile 组件位置（非常重要！）
    logger.info("滚动到验证组件...")
    sb.execute_script('''
        (function() {
            var turnstile = document.querySelector('.cf-turnstile');
            if (turnstile) {
                turnstile.scrollIntoView({behavior: 'smooth', block: 'center'});
            } else {
                // 如果找不到，滚动到页面中间
                window.scrollTo(0, document.body.scrollHeight / 2);
            }
        })();
    ''')
    time.sleep(3)  # 等待滚动和 Turnstile 加载
    
    # 检查是否存在 Turnstile 组件
    has_turnstile = sb.execute_script('''
        (function() {
            return !!(document.querySelector('.cf-turnstile') ||
                      document.querySelector('[data-sitekey]') ||
                      document.querySelector('iframe[src*="challenges.cloudflare"]') ||
                      document.querySelector('iframe[src*="turnstile"]'));
        })();
    ''')
    if not has_turnstile:
        logger.info("未检测到 Turnstile 组件，无需验证")
        return True
    
    logger.info("发现 Turnstile 组件，尝试触发验证...")
    
    # 先尝试使用 uc_gui_click_captcha 触发验证（最多3次）
    for attempt in range(3):
        logger.info(f"Turnstile 验证尝试 {attempt + 1}/3")
        
        try:
            sb.uc_gui_click_captcha()
            logger.info("已调用 uc_gui_click_captcha")
        except Exception as e:
            logger.warning(f"uc_gui_click_captcha 失败: {e}")
        
        # 等待验证完成（最多20秒）
        start_time = time.time()
        timeout = 20
        while time.time() - start_time < timeout:
            # 检查是否有 token 生成
            token_ready = sb.execute_script('''
                (function() {
                    var tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                    if (tokenInput && tokenInput.value && tokenInput.value.length > 20) return true;
                    
                    // 检查 renew 按钮是否已启用
                    var renewBtn = document.querySelector('#renew-btn');
                    if (renewBtn && !renewBtn.disabled) return true;
                    
                    // 检查是否有成功标识
                    var successEl = document.querySelector('#success');
                    if (successEl && getComputedStyle(successEl).display !== 'none') return true;
                    
                    return false;
                })();
            ''')
            if token_ready:
                logger.info(f"✅ Turnstile 验证成功 (第{attempt + 1}次)")
                safe_screenshot(sb, screenshot_path(account_index, f"turnstile-success-{server_id}"), result)
                return True
            
            time.sleep(1)
        
        logger.warning(f"Turnstile 验证尝试 {attempt + 1} 未完成")
        if attempt < 2:
            # 再次滚动到验证组件
            sb.execute_script('''
                (function() {
                    var turnstile = document.querySelector('.cf-turnstile');
                    if (turnstile) turnstile.scrollIntoView({behavior: 'smooth', block: 'center'});
                })();
            ''')
            time.sleep(3)
    
    # 如果3次尝试都失败，最后等待自动完成（30秒）
    logger.info("尝试等待 Turnstile 自动完成...")
    start_time = time.time()
    timeout = 30
    while time.time() - start_time < timeout:
        token_ready = sb.execute_script('''
            (function() {
                var tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                return tokenInput && tokenInput.value && tokenInput.value.length > 20;
            })();
        ''')
        if token_ready:
            logger.info("✅ Turnstile 自动验证成功")
            return True
        time.sleep(2)
    
    logger.error("❌ Turnstile 验证失败")
    safe_screenshot(sb, screenshot_path(account_index, f"turnstile-failed-{server_id}"), result)
    return False

# ================== 登录流程 ==================
def handle_initial_page(sb, account_index: int, result: Dict) -> Optional[str]:
    logger.info("访问登录页...")
    
    # 注入绕过脚本（关键！）
    inject_adblock_bypass(sb)
    
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
    time.sleep(4)
    safe_screenshot(sb, screenshot_path(account_index, "01-initial"), result)

    current_url = sb.get_current_url()
    logger.info(f"当前URL: {mask_url(current_url)}")

    if "/server/index" in current_url:
        logger.info("✅ 已经登录")
        return "already_logged"

    # 检查是否被 AdBlocker 页面拦截（关键！）
    if not check_and_handle_adblocker_page(sb, account_index, result):
        logger.error("AdBlocker 拦截失败")
        safe_screenshot(sb, screenshot_path(account_index, "02-adblocker-blocked"), result)
        return None

    if is_cloudflare_interstitial(sb):
        if not bypass_cloudflare_interstitial(sb):
            safe_screenshot(sb, screenshot_path(account_index, "02-cf-failed"), result)
            return None
        time.sleep(3)
        current_url = sb.get_current_url()
        if "/server/index" in current_url:
            return "already_logged"

    for wait_round in range(3):
        try:
            sb.wait_for_element_visible('#loginformmodel-username', timeout=10)
            logger.info("✅ 找到登录表单")
            return "need_login"
        except TimeoutException:
            logger.info(f"等待表单超时 ({wait_round+1}/3)，检查 CF...")
            if is_cloudflare_interstitial(sb):
                bypass_cloudflare_interstitial(sb, max_attempts=2)
                time.sleep(3)
            else:
                time.sleep(3)

    safe_screenshot(sb, screenshot_path(account_index, "02-no-form"), result)
    logger.error("未找到登录表单")
    return None

def fill_and_submit(sb, email: str, password: str, account_index: int, result: Dict) -> bool:
    logger.info("填写登录信息...")
    sb.type('#loginformmodel-username', email)
    sb.type('#loginformmodel-password', password)
    safe_screenshot(sb, screenshot_path(account_index, "03-form-filled"), result)

    logger.info("提交登录...")
    try:
        sb.click('button[type="submit"].btn-register')
    except:
        try:
            sb.execute_script('document.querySelector("form").submit()')
        except:
            logger.error("提交失败")
            return False

    time.sleep(6)
    current_url = sb.get_current_url()
    logger.info(f"登录后URL: {mask_url(current_url)}")

    if "/user/login" in current_url:
        try:
            err = sb.execute_script('''
                var alert = document.querySelector('.alert-danger, .error-message');
                return alert ? alert.innerText : '';
            ''')
            if err:
                logger.error(f"登录错误: {err}")
        except:
            pass
        safe_screenshot(sb, screenshot_path(account_index, "05-login-failed"), result)
        return False

    logger.info("✅ 登录成功")
    return True

def close_welcome_popup(sb, account_index: int, result: Dict):
    try:
        sb.wait_for_element_visible('.stpd_cmp_form', timeout=5)
        logger.info("发现隐私弹窗，尝试关闭...")
        sb.click('button.stpd_cta_btn', timeout=3)
        time.sleep(1)
        safe_screenshot(sb, screenshot_path(account_index, "06-popup-closed"), result)
        logger.info("弹窗已关闭")
    except:
        logger.debug("无隐私弹窗或已关闭")

# ================== 获取服务器列表 ==================
def get_all_servers(sb, account_index: int, result: Dict) -> List[Tuple[str, str]]:
    logger.info("获取服务器列表...")
    
    # 注入绕过脚本
    inject_adblock_bypass(sb)
    
    sb.open(SERVER_INDEX_URL)
    time.sleep(3)
    
    # 检查 AdBlocker 整页
    if not check_and_handle_adblocker_page(sb, account_index, result):
        logger.error("AdBlocker 拦截，无法获取服务器列表")
        return []
    
    # 处理 AdBlocker 弹窗
    handle_adblocker_popup(sb, account_index, result)
    
    safe_screenshot(sb, screenshot_path(account_index, "07-server-index"), result)
    close_welcome_popup(sb, account_index, result)

    # 滚动加载所有服务器
    last_height = sb.execute_script("return document.body.scrollHeight")
    scroll_attempts = 0
    while scroll_attempts < 5:
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        handle_adblocker_popup(sb, account_index, result)
        new_height = sb.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
        scroll_attempts += 1

    time.sleep(2)
    safe_screenshot(sb, screenshot_path(account_index, "07-server-index-scrolled"), result)

    # 使用 JavaScript 提取服务器信息
    servers = sb.execute_script('''
        (function() {
            var servers = [];
            var cards = document.querySelectorAll('div.server-card');
            cards.forEach(function(card) {
                var titleEl = card.querySelector('h5.server-card-title');
                var manageLink = card.querySelector('a.btn-success');
                if (titleEl && manageLink) {
                    var name = titleEl.innerText.trim();
                    var href = manageLink.getAttribute('href');
                    var match = href.match(/\\/server\\/(\\d+)/);
                    if (match) {
                        servers.push({ id: match[1], name: name });
                    }
                }
            });
            return servers;
        })();
    ''')
    
    if servers is None:
        servers = []
    
    logger.info(f"找到 {len(servers)} 个服务器")
    for s in servers:
        # 隐藏服务器 ID（只在日志中）
        masked_name = mask_server_name(s['name'], s['id'])
        logger.info(f"服务器: {masked_name}")
    return [(s['id'], s['name']) for s in servers]

# ================== 续订单个服务器 ==================
def renew_server(sb, server_id: str, server_name: str, account_index: int, result: Dict) -> Dict[str, Any]:
    """
    续订单个服务器
    
    Returns:
        {
            "id": "xxx",
            "name": "xxx",
            "success": True/False,
            "before": "到期时间（续订前）",
            "after": "到期时间（续订后）"
        }
    """
    # 日志中隐藏服务器 ID
    masked_name = mask_server_name(server_name, server_id)
    logger.info(f"处理服务器 {masked_name}")
    
    server_result = {
        "id": server_id,
        "name": server_name,
        "success": False,
        "before": "",
        "after": ""
    }
    
    # 获取续订前的到期时间
    logger.info(f"获取服务器 {mask_server_id(server_id)} 续订前到期时间...")
    before_expiry = get_server_expiry(sb, server_id)
    if before_expiry:
        server_result["before"] = before_expiry
        logger.info(f"续订前到期时间: {before_expiry}")
    
    # 直接访问续订页面
    renew_url = f"{BASE_URL}/server/{server_id}/renew-basic"
    logger.info(f"访问续订页面: {mask_url(renew_url)}")
    
    # 注入绕过脚本（关键！）
    inject_adblock_bypass(sb)
    
    sb.uc_open_with_reconnect(renew_url, reconnect_time=8)
    time.sleep(5)
    
    # 检查当前 URL
    current_url = sb.get_current_url()
    logger.info(f"当前URL: {mask_url(current_url)}")
    
    # 检查 AdBlocker 整页
    if not check_and_handle_adblocker_page(sb, account_index, result):
        logger.error("AdBlocker 拦截，无法续订")
        safe_screenshot(sb, screenshot_path(account_index, f"08-adblocker-blocked-{server_id}"), result)
        return server_result
    
    # 处理 AdBlocker 弹窗
    handle_adblocker_popup(sb, account_index, result)
    
    # 滚动页面以触发 Turnstile 加载（关键步骤！）
    logger.info("滚动页面以加载验证组件...")
    sb.execute_script('''
        (function() {
            // 先滚动到页面底部
            window.scrollTo(0, document.body.scrollHeight);
        })();
    ''')
    time.sleep(2)
    
    # 再滚动到验证组件位置
    sb.execute_script('''
        (function() {
            var captchaWarning = document.querySelector('#captcha-warning');
            var turnstile = document.querySelector('.cf-turnstile');
            var renewBtn = document.querySelector('#renew-btn');
            
            if (turnstile) {
                turnstile.scrollIntoView({behavior: 'smooth', block: 'center'});
            } else if (captchaWarning) {
                captchaWarning.scrollIntoView({behavior: 'smooth', block: 'center'});
            } else if (renewBtn) {
                renewBtn.scrollIntoView({behavior: 'smooth', block: 'center'});
            } else {
                // 滚动到页面中间
                window.scrollTo(0, document.body.scrollHeight / 2);
            }
        })();
    ''')
    time.sleep(3)
    
    safe_screenshot(sb, screenshot_path(account_index, f"08-renew-page-{server_id}"), result)
    
    # 等待页面加载完成（续订按钮或验证组件出现）
    page_ready = False
    for _ in range(10):
        page_ready = sb.execute_script('''
            (function() {
                return !!(document.querySelector('#renew-btn') || 
                         document.querySelector('.cf-turnstile') ||
                         document.querySelector('#captcha-warning'));
            })();
        ''')
        if page_ready:
            break
        time.sleep(1)
    
    if not page_ready:
        logger.error("续订页面未正确加载")
        safe_screenshot(sb, screenshot_path(account_index, f"08-page-not-ready-{server_id}"), result)
        return server_result
    
    logger.info("✅ 续订页面已加载")
    
    # 处理 Turnstile 验证
    if not handle_turnstile_verification(sb, account_index, result, server_id):
        logger.error("Turnstile 验证失败，跳过此服务器")
        return server_result
    
    # 点击续订按钮
    try:
        # 先滚动到续订按钮
        sb.execute_script('''
            (function() {
                var btn = document.querySelector('#renew-btn');
                if (btn) btn.scrollIntoView({behavior: 'smooth', block: 'center'});
            })();
        ''')
        time.sleep(1)
        
        # 等待按钮可点击（最多15秒）
        btn_enabled = False
        for i in range(15):
            btn_enabled = sb.execute_script('''
                (function() {
                    var btn = document.querySelector('#renew-btn');
                    return btn && !btn.disabled;
                })();
            ''')
            if btn_enabled:
                logger.info(f"续订按钮已启用（等待 {i+1} 秒）")
                break
            time.sleep(1)
        
        if not btn_enabled:
            logger.error("续订按钮未启用")
            safe_screenshot(sb, screenshot_path(account_index, f"09-btn-not-enabled-{server_id}"), result)
            return server_result
        
        # 使用 JavaScript 点击按钮（更可靠）
        clicked = sb.execute_script('''
            (function() {
                var btn = document.querySelector('#renew-btn');
                if (btn && !btn.disabled) {
                    btn.click();
                    return true;
                }
                return false;
            })();
        ''')
        
        if clicked:
            logger.info("✅ 点击 Renew Server Now 按钮（JavaScript）")
        else:
            # 后备方案：使用 SeleniumBase 点击
            try:
                renew_btn = sb.find_element("#renew-btn")
                if renew_btn and renew_btn.is_enabled():
                    renew_btn.click()
                    logger.info("✅ 点击 Renew Server Now 按钮（SeleniumBase）")
                else:
                    logger.error("Renew 按钮不可用")
                    safe_screenshot(sb, screenshot_path(account_index, f"09-btn-disabled-{server_id}"), result)
                    return server_result
            except Exception as e:
                logger.error(f"SeleniumBase 点击失败: {e}")
                safe_screenshot(sb, screenshot_path(account_index, f"09-btn-click-failed-{server_id}"), result)
                return server_result
    except Exception as e:
        logger.error(f"无法点击续订按钮: {e}")
        safe_screenshot(sb, screenshot_path(account_index, f"09-renew-btn-error-{server_id}"), result)
        return server_result
    
    time.sleep(5)
    handle_adblocker_popup(sb, account_index, result)
    safe_screenshot(sb, screenshot_path(account_index, f"10-renew-after-click-{server_id}"), result)
    
    # 检查续订成功弹窗
    try:
        sb.wait_for_element_visible('.swal2-icon-success', timeout=10)
        success_text = sb.execute_script('''
            (function() {
                var el = document.querySelector('#swal2-html-container');
                return el ? el.innerText : '';
            })();
        ''')
        if "renewed" in success_text.lower():
            server_result["success"] = True
            logger.info(f"✅ 服务器 {masked_name} 续订成功")
        safe_screenshot(sb, screenshot_path(account_index, f"11-renew-success-{server_id}"), result)
        # 关闭成功弹窗
        try:
            sb.click('.swal2-confirm')
        except:
            pass
    except Exception as e:
        logger.warning(f"未检测到成功弹窗: {e}")
        # 可能已经续订过，检查页面是否有错误提示
        try:
            error_text = sb.execute_script('''
                (function() {
                    var err = document.querySelector('.swal2-icon-error, .alert-danger');
                    return err ? err.innerText : '';
                })();
            ''')
            if error_text:
                logger.error(f"续订失败: {error_text}")
        except:
            pass
    
    # 获取续订后的到期时间
    if server_result["success"]:
        logger.info(f"获取服务器 {mask_server_id(server_id)} 续订后到期时间...")
        after_expiry = get_server_expiry(sb, server_id)
        if after_expiry:
            server_result["after"] = after_expiry
            logger.info(f"续订后到期时间: {after_expiry}")
    
    return server_result

# ================== 主流程 ==================
def process_account(account_index: int, email: str, password: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    result = {"success": False, "message": "", "screenshots": [], "server_results": []}
    masked = mask_email(email)

    logger.info("=" * 50)
    logger.info(f"处理账号 {account_index}: {masked}")
    logger.info("=" * 50)

    sb_kwargs = {
        "uc": True,
        "test": True,
        "locale": "en",
        "headed": not is_linux(),
        "chromium_arg": "--disable-blink-features=AutomationControlled",
    }
    if proxy:
        sb_kwargs["proxy"] = proxy

    try:
        with SB(**sb_kwargs) as sb:
            status = handle_initial_page(sb, account_index, result)
            if status is None:
                result["message"] = "Cloudflare 绕过失败或 AdBlocker 拦截"
                return result

            if status == "need_login":
                if not fill_and_submit(sb, email, password, account_index, result):
                    result["message"] = "登录失败"
                    return result

            close_welcome_popup(sb, account_index, result)
            handle_adblocker_popup(sb, account_index, result)
            
            # 检查 AdBlocker 整页
            if not check_and_handle_adblocker_page(sb, account_index, result):
                result["message"] = "AdBlocker 拦截，无法继续"
                return result

            servers = get_all_servers(sb, account_index, result)
            
            if not servers:
                # 没有服务器：跳过续订，但不算失败
                result["message"] = "没有找到服务器，跳过续订"
                result["success"] = True
                logger.info("⚠️ 没有找到服务器，跳过续订")
                return result

            server_results = []
            renewed = 0
            for sid, sname in servers:
                # 每次续订前检查 AdBlocker 整页
                check_and_handle_adblocker_page(sb, account_index, result)
                
                sr = renew_server(sb, sid, sname, account_index, result)
                server_results.append(sr)
                if sr["success"]:
                    renewed += 1
                time.sleep(3)

            result["server_results"] = server_results

            # 只有实际续订了服务器才算成功
            if renewed > 0:
                result["success"] = True
                result["message"] = f"续订成功 {renewed}/{len(servers)} 个服务器"
                logger.info(f"✅ 续订成功: {renewed}/{len(servers)}")
            else:
                result["success"] = False
                result["message"] = f"所有服务器续订失败（共 {len(servers)} 个）"
                logger.error(f"❌ 所有服务器续订失败（共 {len(servers)} 个）")
            
            return result
    except Exception as e:
        logger.exception(f"账号处理异常: {e}")
        result["message"] = str(e)
        return result

def main():
    accounts = parse_accounts()
    if not accounts:
        logger.error("未找到账号配置，请设置环境变量 FREEMCSERVER (格式: 邮箱-----密码，每行一个)")
        sys.exit(1)

    proxy = os.environ.get("PROXY_SERVER") or os.environ.get("HY2_URL")
    if proxy and not proxy.startswith("http"):
        logger.warning("PROXY_SERVER 未正确设置，将不使用代理")
        proxy = None

    display = setup_display()
    success_count = 0

    try:
        for i, (email, pwd) in enumerate(accounts, 1):
            result = process_account(i, email, pwd, proxy)
            if result is None:
                result = {"success": False, "message": "未知错误", "screenshots": [], "server_results": []}
            
            if result["success"]:
                success_count += 1
            
            # 发送 TG 通知（包含详细的服务器信息）
            last_screenshot = result["screenshots"][-1] if result.get("screenshots") else None
            notify_telegram(
                i, 
                email,  # TG 通知不隐藏邮箱
                result.get("server_results", []),
                result["success"],
                result.get("message", ""),
                last_screenshot
            )
            
            if i < len(accounts):
                logger.info("等待 15 秒后处理下一个账号...")
                time.sleep(15)

        logger.info(f"处理完成: {success_count}/{len(accounts)} 个账号成功")
        sys.exit(0 if success_count == len(accounts) else 1)
    finally:
        if display:
            display.stop()

if __name__ == "__main__":
    main()
