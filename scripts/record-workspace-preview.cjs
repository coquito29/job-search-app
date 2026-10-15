// Isolated visual verification and downloadable motion demos. Never calls live job APIs.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");
const {spawn, execFileSync} = require("node:child_process");
const {chromium} = require(process.env.WORKSPACE_PLAYWRIGHT || "playwright");
const out = path.resolve(process.env.PREVIEW_OUTPUT || "workspace-preview");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "workspace-motion-"));
const origin = "http://127.0.0.1:5083";
const iso = new Date().toISOString();
const jobs = [
  ["SOC Analyst I", "Example Security Co.", 86],
  ["Technical Support Specialist", "Example Software Co.", 81],
  ["Junior QA Analyst", "Example Product Co.", 74]
].map((a, i) => ({
  title:a[0], company_name:a[1], match_pct:a[2], location:"Remote — United States",
  url:"https://example.invalid/sample-" + i, salary_clean:"$50,000–$70,000",
  source:"Apify", posted:iso, days_old:0, date_fmt:"Today", ats_class:"fast",
  ats_name:"Greenhouse", loc_class:"US", blockers:[], scam_flags:[],
  match_why:"A sample match for demonstrating the workspace",
  description:"Example listing. This preview cannot search or submit applications."
}));
const data = {jobs,total_fetched:3,total_remote:3,total_shown:3,source_counts:{Apify:3},source_errors:{}};
const work = [{company:"Example Company",title:"Support Specialist",start_date:"2024-01",end_date:"",location:"Remote",current:true}];
const fullProfile = {first_name:"Demo",last_name:"Candidate",full_name:"Demo Candidate",email:"candidate@example.invalid",phone:"",work_experience:work,qa_defaults:[]};
const overrides = {
  "/api/profile":{skills:["Cybersecurity","Technical Support","SQL"],summary:"Sample profile for a local preview",settings:{work_experience:work,qa_defaults:[]}},
  "/api/profile/full":fullProfile,
  "/api/cvs":{cvs:[{id:1,filename:"Sample Candidate CV.txt",category:"other",is_default:true,ai_summary:"Sample CV",ai_skills:[],created_at:iso}]},
  "/api/config":{ai_enabled:false,apify_token_set:false}
};
function run(command, args) {
  execFileSync(command, args, {stdio:"inherit",timeout:180000});
}
async function main() {
  fs.mkdirSync(out,{recursive:true});
  const env = {...process.env,APPLICATIONS_DB:path.join(temp,"preview.db")};
  for(const key of ["DATABASE_URL","APIFY_TOKEN","ANTHROPIC_API_KEY","RENDER"]) delete env[key];
  const server = spawn("python",["-m","flask","--app","app","run","--host","127.0.0.1","--port","5083"],{env,stdio:"inherit"});
  let browser;
  try {
    let ready = false;
    for(let i=0;i<60;i++){
      try { if((await fetch(origin+"/api/health")).ok){ready=true;break;} } catch (_) {}
      await new Promise(r=>setTimeout(r,500));
    }
    assert(ready,"Isolated preview server did not start");
    const routes = ["/api/auth/status","/api/profile","/api/profile/full","/api/cvs","/api/config",
      "/api/billing/status","/api/outlook/auth/status","/api/applications/urls","/api/applications",
      "/api/applications/stats","/api/daily-results","/api/autopilot/status","/api/autopilot/attempts",
      "/api/autopilot/queue","/api/autopilot/blockers","/api/stats","/api/cover-letters"];
    const replies = {};
    for(const route of routes) {
      if(overrides[route]) replies[route]=overrides[route];
      else {const r=await fetch(origin+route);assert(r.ok,route+" failed");replies[route]=await r.json();}
    }
    const cdn = "https://cdn.jsdelivr.net/npm/";
    const resources = [
      [cdn+"bootstrap@5.3.3/dist/css/bootstrap.min.css","text/css"],
      [cdn+"bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js","text/javascript"],
      [cdn+"bootstrap-icons@1.11.3/font/bootstrap-icons.min.css","text/css"],
      [cdn+"bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2","font/woff2"]
    ];
    const assets = new Map();
    for(const [url,type] of resources) {
      const r=await fetch(url);assert(r.ok,"Cannot load existing UI dependency");
      assets.set(new URL(url).pathname,{body:Buffer.from(await r.arrayBuffer()),contentType:type});
    }
    const rawHtml=await (await fetch(origin)).text();
    const css=fs.readFileSync("static/workspace.css","utf8");
    const js=fs.readFileSync("static/workspace.js","utf8");
    const font=assets.get(new URL(resources[3][0]).pathname).body.toString("base64");
    const bootstrapCss=assets.get(new URL(resources[0][0]).pathname).body.toString();
    const bootstrapJs=assets.get(new URL(resources[1][0]).pathname).body.toString();
    let iconCss=assets.get(new URL(resources[2][0]).pathname).body.toString();
    iconCss=iconCss.replace(/url\([^)]*bootstrap-icons\.woff2?[^)]*\)/g,"url(data:font/woff2;base64,"+font+")");
    let demo=rawHtml
      .replace(/<link[^>]*rel="manifest"[^>]*>/,"")
      .replace(/<link[^>]*href="https:\/\/cdn\.jsdelivr\.net\/npm\/bootstrap@[^"]*"[^>]*>/,()=>"<style>"+bootstrapCss+"</style>")
      .replace(/<link[^>]*href="https:\/\/cdn\.jsdelivr\.net\/npm\/bootstrap-icons@[^"]*"[^>]*>/,()=>"<style>"+iconCss+"</style>")
      .replace(/<link[^>]*href="\/static\/workspace\.css"[^>]*>/,()=>"<style>"+css+"</style>")
      .replace(/<script[^>]*src="\/static\/workspace\.js"[^>]*><\/script>/,()=>"<script>"+js+"</script>")
      .replace(/<script[^>]*src="https:\/\/cdn\.jsdelivr\.net\/npm\/bootstrap@[^"]*"[^>]*><\/script>/,()=>"<script>"+bootstrapJs+"</script>");
    const seed = {ts:Date.now(),skills:["Cybersecurity","Technical Support","SQL"],time_range:"7d",data};
    const mock = "window.fetch=async function(url,options){var key=new URL(String(url),location.href).pathname;var method=(options&&options.method||'GET').toUpperCase();return new Response(JSON.stringify(method==='GET'&&Object.prototype.hasOwnProperty.call(window.__previewReplies,key)?window.__previewReplies[key]:{error:'Preview only: live actions are disabled'}),{status:method==='GET'?200:405,headers:{'Content-Type':'application/json'}});};window.open=function(){return null;};document.addEventListener('click',function(e){var a=e.target.closest('a');if(a&&a.getAttribute('href')&&!a.getAttribute('href').startsWith('#'))e.preventDefault();},true);try{localStorage.setItem('last_search_results',JSON.stringify(window.__previewSeed));localStorage.setItem('job_search_tab','today');localStorage.removeItem('workspace_motion_paused');}catch(e){};";
    demo=demo.replace("<head>","<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'none';\"><script>window.__previewReplies="+JSON.stringify(replies)+";window.__previewSeed="+JSON.stringify(seed)+";"+mock+"</script>");
    fs.writeFileSync(path.join(out,"interactive-preview.html"),demo);
    browser=await chromium.launch({headless:true});
    const errors=[],blocked=[];
    async function context(width,height,video) {
      const c=await browser.newContext({viewport:{width,height},deviceScaleFactor:1,...(video?{recordVideo:{dir:path.join(temp,"videos"),size:{width,height}}}:{})});
      await c.addInitScript(seed=>{
        localStorage.setItem("last_search_results",JSON.stringify(seed));
        localStorage.setItem("job_search_tab","today");
        localStorage.removeItem("workspace_motion_paused");
      },seed);
      await c.route("**/*",r=>{
        const u=new URL(r.request().url()),method=r.request().method();
        if(method!=="GET"){blocked.push(method+" "+u.pathname);return r.abort();}
        if(u.hostname==="127.0.0.1"){
          if(u.pathname.startsWith("/api/"))return r.fulfill({json:replies[u.pathname]||{}});
          return r.continue();
        }
        if(u.hostname==="cdn.jsdelivr.net"&&assets.has(u.pathname))return r.fulfill({status:200,...assets.get(u.pathname)});
        blocked.push(method+" "+u.hostname+u.pathname);return r.abort();
      });
      const p=await c.newPage();p.on("pageerror",e=>errors.push(e.message));p.setDefaultTimeout(10000);
      await p.goto(origin,{waitUntil:"networkidle"});
      return {c,p};
    }
    const {c,p}=await context(1280,900,false);
    const transform=()=>p.locator(".sheet-front").evaluate(e=>getComputedStyle(e).transform);
    const first=await transform();await p.waitForTimeout(500);assert.notEqual(await transform(),first,"Hero should visibly animate");
    await p.locator("#workspaceMotionToggle").click();const paused=await transform();await p.waitForTimeout(350);assert.equal(await transform(),paused,"Pause control must freeze decorative motion");
    await p.reload({waitUntil:"networkidle"});assert.equal(await p.locator("#workspaceMotionToggle").getAttribute("aria-pressed"),"true","Pause preference must persist");
    await p.locator("#workspaceMotionToggle").click();
    for(const width of [320,390,768,1280]){
      await p.setViewportSize({width,height:900});
      for(const tab of ["today","jobs","setup","tracker"]){
        await p.locator('.step-item[data-tab="'+tab+'"]').click();
        assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"Horizontal overflow on "+tab+" at "+width);
      }
    }
    await p.locator('[data-tab="today"]').click();
    await p.emulateMedia({reducedMotion:"reduce"});
    assert.equal(await p.locator(".sheet-front").evaluate(e=>getComputedStyle(e).animationName),"none");
    assert(await p.locator("#workspaceMotionToggle").isDisabled());
    await p.emulateMedia({reducedMotion:"no-preference"});
    await p.waitForFunction(()=>!document.getElementById("workspaceMotionToggle").disabled);
    assert(!(await p.locator("#workspaceMotionToggle").isDisabled()));
    await c.close();
    // Record genuine CSS animation from the rendered application, not generated imagery.
    for(const [label,width,height,duration] of [["desktop",1280,900,9000],["mobile",390,844,6500]]){
      const {c,p}=await context(width,height,true);
      await p.waitForTimeout(duration);
      await p.screenshot({path:path.join(out,label+".png")});
      const video=p.video();await c.close();
      const input=path.join(temp,label+".webm");await video.saveAs(input);
      run("ffmpeg",["-y","-loglevel","error","-ss","1","-i",input,"-t",label==="desktop"?"8":"5.5","-an","-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart",path.join(out,label+"-motion.mp4")]);
      if(label==="desktop")run("ffmpeg",["-y","-loglevel","error","-ss","1","-i",input,"-t","8","-filter_complex","fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4","-loop","0",path.join(out,"workspace-motion.gif")]);
    }
    // The downloadable HTML must also run offline without contacting any service.
    const offline=await browser.newContext({viewport:{width:1280,height:900}});
    const page=await offline.newPage();const requests=[];
    page.on("request",r=>{if(r.url().startsWith("http"))requests.push(r.url());});
    await page.goto("file://"+path.join(out,"interactive-preview.html"),{waitUntil:"networkidle"});
    assert(await page.locator(".workspace-hero").isVisible());
    const a=await page.locator(".sheet-front").evaluate(e=>getComputedStyle(e).transform);
    await page.waitForTimeout(350);
    assert.notEqual(await page.locator(".sheet-front").evaluate(e=>getComputedStyle(e).transform),a);
    await page.locator('[data-tab="jobs"]').click();
    assert.equal(await page.locator("#jobsList .job-card").count(),3);
    assert.deepEqual(requests,[],"Standalone preview must not request remote assets");
    await offline.close();
    assert.deepEqual(errors,[]);assert.deepEqual(blocked,[]);
    fs.writeFileSync(path.join(out,"verification.json"),JSON.stringify({passed:true,widths:[320,390,768,1280],checks:["animation changes over time","pause control","persisted pause","reduced motion","responsive overflow","offline interactive preview"],sampleDataOnly:true},null,2));
    console.log("PASS: actual desktop/mobile motion, pause/resume, reduced motion, responsive layout and self-contained offline preview.");
  } finally {
    if(browser)await browser.close();
    server.kill("SIGTERM");
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
