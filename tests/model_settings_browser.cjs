/* NODE_PATH must contain Playwright. Use model_settings_browser_server.py. */
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
  const info=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  try{
    await page.goto(info.origin+'/?token='+info.token);
    await page.locator('#model-settings-open').click();
    await page.locator('.auth-card').first().waitFor();
    assert.equal(await page.locator('.role-model-row').count(),9);
    await page.getByLabel('기본 연결 방식',{exact:true}).selectOption('codex_oauth');
    await page.getByLabel('의미 초안 작성 연결',{exact:true}).selectOption('claude_oauth');
    await page.getByLabel('의미 초안 작성 모델',{exact:true}).selectOption('opus');
    await page.getByLabel('의미 초안 작성 추론',{exact:true}).selectOption('MEDIUM');
    await page.getByLabel('통사·범위 검수 모델',{exact:true}).selectOption('__custom');
    await page.getByLabel('통사·범위 검수 모델 직접 입력',{exact:true}).fill('custom-codex-model');
    await page.locator('#model-settings-save').click();
    await page.getByText('저장됨 · 다음 요청부터 적용됩니다',{exact:true}).waitFor();
    await page.locator('#model-settings-dialog').evaluate(e=>e.scrollTop=0);
    await page.screenshot({path:path.join(path.dirname(process.argv[2]),'model-settings-desktop.png'),fullPage:true});
    await page.locator('#model-settings-close').click();
    await page.reload();
    await page.locator('#model-settings-open').click();
    await page.locator('.role-model-row').first().waitFor();
    assert.equal(await page.getByLabel('기본 연결 방식',{exact:true}).inputValue(),'codex_oauth');
    assert.equal(await page.getByLabel('의미 초안 작성 모델',{exact:true}).inputValue(),'opus');
    assert.equal(await page.getByLabel('통사·범위 검수 모델',{exact:true}).inputValue(),'custom-codex-model');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(path.dirname(process.argv[2]),'model-settings-mobile.png'),fullPage:true});
    assert(await page.locator('#model-settings-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+1),'no horizontal overflow');
    await page.locator('#models-apply-all').click();
    assert.equal(await page.getByLabel('의미 초안 작성 연결',{exact:true}).inputValue(),'');
    assert.equal(await page.getByLabel('통사·범위 검수 모델',{exact:true}).inputValue(),'');
    assert.deepEqual(errors,[]);
    console.log('Model settings browser checks passed: persistence, mixed roles, custom ID, bulk reset, mobile layout.');
  }finally{
    await page.request.post(info.origin+'/api/shutdown',{headers:{'X-Claim-Request':'1'},data:{}});
    await browser.close();
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
