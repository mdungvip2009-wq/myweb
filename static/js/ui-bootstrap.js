(function(){
  const CSS = `
  .lang-flag{width:32px;height:32px;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;border-radius:50%;border:2px solid var(--line,#183558);overflow:hidden;box-sizing:border-box;transition:.2s ease;background:#213247}
  .lang-flag:hover{border-color:var(--accent,#2dd4bf);transform:scale(1.05)}
  .lang-flag svg{width:100%;height:100%;display:block;pointer-events:none}
  .lang-menu{position:absolute;top:34px;right:0;background:#223149;border:1px solid #2a3f5e;border-radius:12px;padding:8px 10px;box-shadow:0 10px 25px rgba(0,0,0,.35);display:none;z-index:300}
  .lang-menu.show{display:block}
  .user-profile{position:relative}
  .user-menu{position:absolute;top:calc(100% + 8px);right:0;background:#223149;border:1px solid #2a3f5e;border-radius:12px;padding:12px;min-width:220px;box-shadow:0 10px 25px rgba(0,0,0,.35);display:none;z-index:320}
  .user-menu.show{display:block}
  .user-menu-header{padding:8px 0;border-bottom:1px solid #2a3f5e;margin-bottom:8px}
  .user-menu-name{font-size:14px;font-weight:700;color:#e8f2ff;margin-bottom:4px}
  .user-menu-email{font-size:12px;color:#9aa3ab}
  .user-menu-item{display:block;width:100%;text-align:left;background:transparent;border:none;color:#e8f2ff;padding:10px 12px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;transition:background .2s ease}
  .user-menu-item:hover{background:#2a3b54}
  .user-menu-item.logout{color:#ff6b6b;margin-top:4px;border-top:1px solid #2a3f5e;padding-top:12px}
  .sidebar-footer{margin-top:auto;background:#213247;border:1px solid #2a3f5e;border-radius:14px;padding:10px;color:#cfe0ff}
  .sidebar-footer .footer-title{font-weight:800;font-size:12px;margin-bottom:8px;text-align:center}
  .sidebar-footer .footer-logo{display:flex;align-items:center;justify-content:center;gap:10px}
  .sidebar-footer .footer-logo img{height:40px;width:auto;object-fit:contain;display:block}
  `;

  function injectStyle(){
    if(document.getElementById('ui-bootstrap-style')) return;
    const s=document.createElement('style'); s.id='ui-bootstrap-style'; s.textContent=CSS; document.head.appendChild(s);
  }

  function qs(sel,root=document){ return root.querySelector(sel); }
  function qsa(sel,root=document){ return Array.from(root.querySelectorAll(sel)); }
  function el(tag,attrs={},children=[]){ const e=document.createElement(tag); Object.entries(attrs||{}).forEach(([k,v])=>{ if(k==='class') e.className=v; else if(k==='style') e.setAttribute('style',v); else if(v!=null) e.setAttribute(k,v); }); (children||[]).forEach(c=>{ if(typeof c==='string') e.insertAdjacentHTML('beforeend',c); else if(c) e.appendChild(c);}); return e; }

  function ensureLangControls(){
    let headerControls = qs('.header-controls');
    const headerUserInfo = qs('.header-user-info');
    if(!headerControls && headerUserInfo){ headerControls = el('div',{class:'header-controls', 'aria-label':'Chọn ngôn ngữ', style:'position:relative'}); headerUserInfo.prepend(headerControls); }
    if(!headerControls) return;
    if(!qs('#lang-toggle', headerControls)){
      headerControls.appendChild(el('span',{class:'lang-flag',id:'lang-toggle',title:'Chọn ngôn ngữ'}));
    }
    if(!qs('#lang-menu', headerControls)){
      const menu = el('div',{class:'lang-menu',id:'lang-menu',role:'menu'});
      menu.appendChild(el('button',{'type':'button','data-lang':'en',style:'display:block;width:160px;text-align:left;background:transparent;border:none;color:#e8f2ff;padding:8px 10px;border-radius:8px;cursor:pointer'},['Tiếng Anh']));
      menu.appendChild(el('button',{'type':'button','data-lang':'vn',style:'display:block;width:160px;text-align:left;background:transparent;border:none;color:#e8f2ff;padding:8px 10px;border-radius:8px;cursor:pointer'},['Tiếng Việt']));
      headerControls.appendChild(menu);
    }
  }

  function ensureUserMenu(){
    const profile = qs('#user-profile') || qs('.user-profile');
    if(!profile) return;
    // remove old sidebar logout buttons
    qsa('#btn-logout, .menu-logout').forEach(x=>x.remove());

    if(!qs('.user-menu', profile)){
      const menu = el('div',{class:'user-menu',id:'user-menu'},[
        el('div',{class:'user-menu-header'},[
          el('div',{class:'user-menu-name',id:'user-name'},['Loading...']),
          el('div',{class:'user-menu-email',id:'user-email'},['Loading...'])
        ]),
        el('button',{type:'button',class:'user-menu-item logout',id:'btn-logout-menu'},['Đăng xuất'])
      ]);
      profile.appendChild(menu);
    }

    // toggle
    profile.addEventListener('click', (e)=>{ e.stopPropagation(); qs('.user-menu',profile)?.classList.toggle('show'); });
    document.addEventListener('click', (e)=>{ if(!e.target.closest('#user-profile') && !e.target.closest('.user-profile')) qs('.user-menu',profile)?.classList.remove('show'); });

    // bind logout
    qs('#btn-logout-menu')?.addEventListener('click', async ()=>{ try{ await fetch('/api/logout',{method:'POST'});}catch{} try{ localStorage.removeItem('token'); sessionStorage.clear(); }catch{} location.replace('/login'); });
  }

  function ensureSidebarFooter(){
    const sb = qs('#sidebar'); if(!sb) return;
    if(!qs('.sidebar-footer', sb)){
      const footer = el('div',{class:'sidebar-footer'},[
        el('div',{class:'footer-title'},['Tài khoản bạn liên kết']),
        el('div',{class:'footer-logo'},[
          el('img',{src:'https://fintech3.net/static/media/logo.8490529fc9b2fd89a0ad.png', alt:'Linked account logo'})
        ])
      ]);
      sb.appendChild(footer);
    }else{
      // ensure no extra brand text
      const logo = qs('.sidebar-footer .footer-logo', sb);
      if(logo){
        qsa('span', logo).forEach(s=>s.remove());
        const img = qs('img', logo);
        if(img){ img.style.height='40px'; img.style.width='auto'; }
      }
    }
  }

  async function loadUserInfo(){
    const token = localStorage.getItem('token'); if(!token) return;
    try{
      const r = await fetch('/api/user/getProfile',{method:'POST', headers:{'Authorization':'Bearer '+token}});
      const js = await r.json();
      if(js && js.status && js.data){
        const {userName,email} = js.data;
        const nameEl = qs('#user-name'); if(nameEl) nameEl.textContent = userName||'User';
        const emailEl = qs('#user-email'); if(emailEl) emailEl.textContent = email||'';
      }
    }catch{}
  }

  function init(){
    injectStyle();
    ensureLangControls();
    ensureUserMenu();
    ensureSidebarFooter();
    loadUserInfo();
  }

  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
