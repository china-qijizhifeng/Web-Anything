document.addEventListener('DOMContentLoaded',()=>{
  const gov=document.querySelector('.how-link'), details=document.querySelector('#gov-details');
  if(gov&&details) gov.addEventListener('click',()=>{const open=gov.getAttribute('aria-expanded')==='true';gov.setAttribute('aria-expanded',String(!open));details.hidden=open});
  const menu=document.querySelector('.menu-toggle'), nav=document.querySelector('#primary-nav');
  const previewToggle=document.querySelector('.preview-toggle');
  if(previewToggle)previewToggle.addEventListener('click',()=>{const open=previewToggle.getAttribute('aria-expanded')==='true';previewToggle.setAttribute('aria-expanded',String(!open));previewToggle.querySelector('span').textContent=open?'+':'−';document.getElementById(previewToggle.getAttribute('aria-controls')).hidden=open});
  if(menu&&nav) menu.addEventListener('click',()=>{const open=menu.getAttribute('aria-expanded')==='true';menu.setAttribute('aria-expanded',String(!open));nav.classList.toggle('nav-open',!open)});
  const feedback=document.querySelector('#feedback-dialog'), feedbackButton=document.querySelector('.feedback'), feedbackForm=document.querySelector('#feedback-form'), comments=document.querySelector('#feedback-comments'), length=document.querySelector('#feedback-length');
  if(feedback&&feedbackButton){feedbackButton.addEventListener('click',()=>feedback.showModal());feedback.addEventListener('click',event=>{if(event.target===feedback)feedback.close()})}
  if(comments&&length) comments.addEventListener('input',()=>{length.textContent=`${140-comments.value.length} characters allowed`});
  if(feedbackForm&&feedback){feedbackForm.addEventListener('submit',event=>{event.preventDefault();feedbackForm.innerHTML='<p class="feedback-thanks" role="status">Thank you for your feedback.</p><button class="feedback-submit" type="button" data-close-feedback>Close</button>';feedbackForm.querySelector('[data-close-feedback]').addEventListener('click',()=>feedback.close())})}
  const filter=document.querySelector('#component-filter');
  if(filter){const cards=[...document.querySelectorAll('.component-card')], count=document.querySelector('#component-count'); const update=()=>{const q=filter.value.trim().toLowerCase();let shown=0;cards.forEach(c=>{const visible=!q||c.dataset.search.includes(q);c.hidden=!visible;if(visible)shown++});count.textContent=shown};filter.addEventListener('input',update);update()}
  const setExpanded=(button,open)=>{button.setAttribute('aria-expanded',String(open));button.querySelector('.toggle-icon').textContent=open?'−':'+';document.getElementById(button.getAttribute('aria-controls')).hidden=!open};
  document.querySelectorAll('.accordion-item button').forEach(btn=>btn.addEventListener('click',()=>{const open=btn.getAttribute('aria-expanded')==='true',group=btn.closest('.accordion-group');if(!open&&group.dataset.multiselectable!=='true')group.querySelectorAll('button').forEach(other=>setExpanded(other,false));setExpanded(btn,!open)}));
});
