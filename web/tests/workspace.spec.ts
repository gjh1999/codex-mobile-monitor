import {test,expect} from '@playwright/test';
for(const [name,width,height] of [['desktop',1440,1000],['iphone',390,844]] as const){
test(`${name}: tasks, drafts, approvals and layout`,async({page})=>{
 await page.setViewportSize({width,height});
 const sent:Record<string,unknown>[]=[];
 await page.route('**/api/**',async route=>{
  const url=new URL(route.request().url());
  let body:unknown={ok:true};
  if(url.pathname==='/api/state')body={connection:'connected',threads:[{id:'task1',title:'优化项目首页与移动端体验',status:{type:'active'}},{id:'task2',title:'完善自动推进与消息通知',status:{type:'idle'}}],controls:{tasks:[{threadId:'task1',canControl:true,managed:true}],operations:[]},approvals:[{token:'approval1',method:'item/commandExecution/requestApproval',expires:Date.now()/1000+300,details:{threadId:'task1',command:'npm run test',cwd:'D:/Projects/demo'}}],quota:{default:{primary:{usedPercent:25,resetsAt:1900000000}}}};
  if(url.pathname.startsWith('/api/conversation'))body={messages:[{role:'user',text:'请先检查首页布局，再完善移动端。'},{role:'assistant',text:'已经完成布局检查。接下来会调整小屏幕下的消息区域，并验证审批操作。'}]};
  if(url.pathname==='/api/operations'){sent.push(route.request().postDataJSON());body={id:'operation1'};}
  if(url.pathname==='/api/approvals'){sent.push(route.request().postDataJSON());body={result:'allowed'};}
  await route.fulfill({json:body});
 });
 await page.goto('/');
 await expect(page).toHaveTitle('澜汐');
 const cover = page.locator('.cover');
 await expect(cover).toBeVisible();
 expect(await cover.evaluate(async element => {
  const background = getComputedStyle(element).backgroundImage;
  const source = background.match(/url\("([^"]+)"\)/)?.[1];
  if (!source) return 0;
  const image = new Image();
  image.src = source;
  await image.decode();
  return image.naturalWidth;
 })).toBe(2560);
 if (name === 'iphone') await page.screenshot({path:'test-results/lanxi-v1/iphone-tasks.png',fullPage:true});
 await page.getByRole('button',{name:/优化项目首页/}).click();
 await expect(page.getByText('已经完成布局检查。',{exact:false})).toBeVisible();
 await page.getByRole('textbox',{name:'任务指令'}).fill('继续验证移动端');
 await page.getByRole('button',{name:'发送指令',exact:true}).click();
 await expect.poll(()=>sent.length).toBe(1);
 expect(sent[0].text).toBe('继续验证移动端');
 await expect(page.getByRole('textbox',{name:'任务指令'})).toHaveValue('');
 await page.getByRole('textbox',{name:'任务指令'}).fill('保留草稿');
 if (name === 'iphone') await page.getByRole('button',{name:'返回任务'}).click();
 await page.getByRole('button',{name:/完善自动推进与消息通知/}).click();
 await expect(page.getByRole('textbox',{name:'任务指令'})).toHaveValue('');
 if (name === 'iphone') await page.getByRole('button',{name:'返回任务'}).click();
 await page.getByRole('button',{name:/优化项目首页/}).click();
 await expect(page.getByRole('textbox',{name:'任务指令'})).toHaveValue('保留草稿');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await expect.poll(() => page.locator('.chat').evaluate(element => getComputedStyle(element).transform)).toBe('none');
 await page.screenshot({path:`test-results/lanxi-v1/${name}-workspace.png`,fullPage:true});
 await page.getByRole('button',{name:/审批中心/}).click();
 const approval = page.getByRole('button',{name:/npm run test/});
 await approval.click();
 const dialog = page.getByRole('dialog',{name:'运行命令'});
 await expect(dialog.getByText('D:/Projects/demo',{exact:false})).toBeVisible();
 if (name === 'iphone') {
  await page.emulateMedia({reducedMotion:'reduce'});
  const handle = await dialog.locator('.sheet-handle').boundingBox();
  expect(handle).not.toBeNull();
  const x = handle!.x + handle!.width / 2;
  const y = handle!.y + handle!.height / 2;
  await page.mouse.move(x,y);
  await page.mouse.down();
  await page.mouse.move(x,y+45);
  await expect.poll(() => dialog.evaluate(element => getComputedStyle(element).translate)).toBe('0px 45px');
  await page.mouse.move(x,y+5);
  await page.mouse.up();
  await expect(dialog).toBeVisible();
  await expect.poll(() => dialog.evaluate(element => getComputedStyle(element).translate)).toBe('0px');
 }
 await dialog.getByRole('button',{name:'允许一次'}).click();
 await expect.poll(()=>sent.length).toBe(2);
 expect(sent[1]).toEqual({token:'approval1',allow:true});
 await page.getByRole('button',{name:'账户额度'}).click();
 await expect(page.getByText('剩余 75%')).toBeVisible();
});
}
