import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';

const source=readFileSync(new URL('../src/sp_mcp/ui/app.js',import.meta.url),'utf8');
const esc=source.split('\n').find(line=>line.startsWith('const esc ='));
const functions=source.slice(source.indexOf('function textHtml(text){'),source.indexOf('async function renderText(){'));
const context=vm.createContext({});
vm.runInContext(esc+'\n'+functions,context);

test('Markdown tables render real cells and escape source HTML',()=>{
  const html=context.textHtml('| Величина | Значение |\n| --- | --- |\n| Учебная | 1,25 |\n| <img src=x onerror=bad()> | 2 |');
  assert.match(html,/<table>/);
  assert.match(html,/<th[^>]*>Величина<\/th>/);
  assert.match(html,/<td[^>]*>1,25<\/td>/);
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
});
test('source grid spacing stays in a preformatted block',()=>{
  const html=context.blocksHtml([{kind:'preformatted',display_text:'│  A  │  1,25  │',text:'│ A │ 1,25 │'}]);
  assert.match(html,/<pre/);
  assert.ok(html.includes('│  A  │  1,25  │'));
});
test('native source table keeps merged cells without executing source content',()=>{
  const html=context.blocksHtml([{kind:'table',caption:'Учебная таблица',table_index:0,total_rows:1,rows:[[{text:'<script>bad()</script>',rowspan:2,colspan:3,header:true}]]}]);
  assert.match(html,/rowspan="2" colspan="3"/);
  assert.ok(!html.includes('<script>'));
  assert.match(html,/data-show-table="0"/);
});
