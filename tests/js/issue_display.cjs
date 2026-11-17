const assert=require('node:assert/strict');
const {issueGroups}=require('../../src/food_record_digitizer/static/review_flow.js');
const content={items:[{item_id:'R1::i1',food_name:{normalized:'Rice'},event_id:'e1'},{item_id:'R1::i2',description:{normalized:'Beans'},event_id:'e1'}],events:[{event_id:'e1',day_id:'d1'}],issues:[
 {code:'inherited_value',target:'record.items[0].preparation',message:'Confirm shared cooking method.'},
 {code:'inherited_value',target:'record.items[1].preparation',message:'Confirm shared cooking method.'},
 {code:'date_needs_confirmation',target:'d1',message:'Confirm the intake date.'},
 {code:'quantity_check',target:'R1::i1.amount',message:'Check record.items[0].amount.'}
]};
const saved=JSON.stringify(content),groups=issueGroups(content,['date_needs_confirmation']);
assert.equal(groups.length,3);assert.equal(groups[0].blocking,true);assert.deepEqual(groups[0].items,[0,1]);
assert.deepEqual(groups[1].items,[0,1]);assert.deepEqual(groups[1].issues,[0,1]);assert.equal(groups[1].title,'Preparation');
assert.equal(groups[2].message,'Check Rice.');assert.equal(JSON.stringify(content),saved);
console.log('Issue display grouping, field labels, targeting and source immutability passed');
