import os
import subprocess
import time
from playwright.sync_api import sync_playwright

def run_concurrent_login_test():
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    user_profile = os.environ.get("USERPROFILE", "C:\\Users\\limue")
    
    ports = [9222, 9223, 9224]
    processes = []
    
    # 1. Start Chrome instances
    for port in ports:
        user_data_dir = os.path.join(user_profile, f"chrome-cdp-profile-{port}")
        cmd = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--headless=new"
        ]
        print(f"Starting Chrome on port {port}...")
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p)
    
    # Wait for Chrome to initialize
    time.sleep(5)
    
    # 2. Run the worker which triggers auto-login for all CDP URLs concurrently
    env = os.environ.copy()
    cdp_urls = ",".join([f"http://127.0.0.1:{p}" for p in ports])
    env["CDP_URLS"] = cdp_urls
    
    print(f"Running worker with CDP_URLS={cdp_urls}")
    worker_proc = subprocess.Popen(
        ["python", "-m", "worker.main"],
        env=env,
        cwd=r"C:\Users\limue\Documents\Projects\AiraHost\airahost-main",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    # We will read worker logs to check if logins are done or wait for a timeout
    timeout = 180
    start_time = time.time()
    login_completed_count = 0
    
    # Non-blocking log reading or simply sleeping
    # Since we can just sleep and assume it finishes in 180s, let's just do a simple wait.
    # To be more robust, we can poll the CDP URLs directly using Playwright.
    print("Waiting for auto-login to complete (up to 180s)...")
    time.sleep(60)
    
    # 3. Inspect if the browser opened the right page and is logged in
    with sync_playwright() as pw:
        for port in ports:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else None
                
                if page:
                    url = page.url
                    print(f"Port {port} URL: {url}")
                    
                    if "login" in url.lower():
                        print(f"Port {port} FAILED: Still on login page")
                    else:
                        print(f"Port {port} SUCCESS: Logged in successfully!")
                else:
                    print(f"Port {port} FAILED: No pages open")
                
                browser.close()
            except Exception as e:
                print(f"Failed to inspect port {port}: {e}")
                
    # 4. Cleanup
    print("Terminating worker and Chrome instances...")
    worker_proc.terminate()
    try:
        worker_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        worker_proc.kill()
        
    for p in processes:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()

if __name__ == "__main__":
    run_concurrent_login_test()
