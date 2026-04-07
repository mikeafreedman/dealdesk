#!/usr/bin/env python3
"""Inject Comparable Market Data (Section 11) into fp_underwriting_FINAL_v7.html"""

with open('fp_underwriting_FINAL_v7.html', 'r', encoding='utf-8') as f:
    content = f.read()

COMP_CARD = '''
        <!-- ── Section 11 — Comparable Market Data ─────────────────────── -->
        <div class="card" id="card-comps">
          <div class="card-head" onclick="toggleCard(this)">
            <div class="card-head-left"><div><div class="card-title">Comparable Market Data</div><div class="card-sub">Rent, commercial &amp; sale comps — extracted from uploaded OM or entered manually</div></div></div>
            <div class="card-toggle open">&#9660;</div>
          </div>
          <div class="card-body">

            <!-- Note banner -->
            <div style="background:#f0ebe0;border-left:3px solid #8B6914;padding:10px 14px;margin-bottom:18px;font-size:12px;color:#5C3D26;border-radius:0 4px 4px 0;">
              If you upload an Offering Memorandum, comps will be extracted automatically and pre-filled below. Manual entries override extracted values.
            </div>

            <!-- ── 11.1 Residential Rent Comps ── -->
            <div class="section-label" style="margin-bottom:10px;">11.1 — Residential Rent Comparables <span style="font-weight:400;color:#888;">(max 8)</span></div>
            <div id="rent-comps-container"></div>
            <button type="button" class="btn-add-comp" onclick="addComp('rent')" id="btn-add-rent">+ Add Rent Comp</button>

            <div style="margin-top:22px;margin-bottom:10px;border-top:1px solid #ddd;padding-top:16px;">
              <!-- ── 11.2 Commercial Rent Comps ── -->
              <div class="section-label" style="margin-bottom:10px;">11.2 — Commercial Rent Comparables <span style="font-weight:400;color:#888;">(max 5)</span></div>
              <div id="commercial-comps-container"></div>
              <button type="button" class="btn-add-comp" onclick="addComp('commercial')" id="btn-add-commercial">+ Add Commercial Comp</button>
            </div>

            <div style="margin-top:22px;margin-bottom:10px;border-top:1px solid #ddd;padding-top:16px;">
              <!-- ── 11.3 Sale Comps ── -->
              <div class="section-label" style="margin-bottom:10px;">11.3 — Sale Comparables <span style="font-weight:400;color:#888;">(max 5)</span></div>
              <div id="sale-comps-container"></div>
              <button type="button" class="btn-add-comp" onclick="addComp('sale')" id="btn-add-sale">+ Add Sale Comp</button>
            </div>

          </div>
        </div>
        <!-- ── /Section 11 — Comparable Market Data ─────────────────────── -->
'''

COMP_JS = r'''
        // ── Comp Entry Logic ─────────────────────────────────────────────
        const COMP_LIMITS = { rent: 8, commercial: 5, sale: 5 };
        const compCounts  = { rent: 0, commercial: 0, sale: 0 };

        function addComp(type, prefill) {
          const limit = COMP_LIMITS[type];
          if (compCounts[type] >= limit) {
            alert('Maximum ' + limit + ' ' + type + ' comps allowed.');
            return;
          }
          compCounts[type]++;
          const n = compCounts[type];
          const container = document.getElementById(type + '-comps-container');
          const div = document.createElement('div');
          div.className = 'comp-row';
          div.id = type + '-comp-' + n;
          div.innerHTML = buildCompFields(type, n, prefill || {});
          container.appendChild(div);
          if (compCounts[type] >= limit) {
            document.getElementById('btn-add-' + type).style.display = 'none';
          }
        }

        function removeComp(type, n) {
          const el = document.getElementById(type + '-comp-' + n);
          if (el) el.remove();
          compCounts[type]--;
          document.getElementById('btn-add-' + type).style.display = '';
        }

        function buildCompFields(type, n, pre) {
          const label = type === 'rent' ? 'Rent Comp' : type === 'commercial' ? 'Commercial Comp' : 'Sale Comp';
          let fields = '';
          if (type === 'rent') {
            fields = `
              <div class="fg2">
                <div class="field"><label>Address</label><input class="fi" type="text" id="rent_comp_${n}_address" placeholder="123 Main St, Philadelphia PA" value="${pre.address||''}"></div>
                <div class="field"><label>Distance (mi)</label><input class="fi" type="number" step="0.1" id="rent_comp_${n}_distance_miles" placeholder="0.3" value="${pre.distance_miles||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Unit Type</label>
                  <select class="fi" id="rent_comp_${n}_unit_type">
                    <option value="">— Select —</option>
                    <option value="Studio" ${pre.unit_type==='Studio'?'selected':''}>Studio</option>
                    <option value="1BR" ${pre.unit_type==='1BR'?'selected':''}>1BR</option>
                    <option value="2BR" ${pre.unit_type==='2BR'?'selected':''}>2BR</option>
                    <option value="3BR" ${pre.unit_type==='3BR'?'selected':''}>3BR</option>
                    <option value="4BR+" ${pre.unit_type==='4BR+'?'selected':''}>4BR+</option>
                  </select>
                </div>
                <div class="field"><label>Beds</label><input class="fi" type="number" id="rent_comp_${n}_beds" placeholder="2" value="${pre.beds||''}"></div>
                <div class="field"><label>Baths</label><input class="fi" type="number" step="0.5" id="rent_comp_${n}_baths" placeholder="1" value="${pre.baths||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Sq Ft</label><input class="fi" type="number" id="rent_comp_${n}_sq_ft" placeholder="850" value="${pre.sq_ft||''}"></div>
                <div class="field"><label>Monthly Rent ($)</label><input class="fi" type="number" id="rent_comp_${n}_monthly_rent" placeholder="1800" value="${pre.monthly_rent||''}"></div>
                <div class="field"><label>Rent/SF ($)</label><input class="fi" type="number" step="0.01" id="rent_comp_${n}_rent_per_sf" placeholder="2.12" value="${pre.rent_per_sf||''}"></div>
              </div>
              <div class="fg2">
                <div class="field"><label>Lease Date</label><input class="fi" type="date" id="rent_comp_${n}_lease_date" value="${pre.lease_date||''}"></div>
                <div class="field"><label>Source</label><input class="fi" type="text" id="rent_comp_${n}_source" placeholder="CoStar, Redfin, Manual entry…" value="${pre.source||''}"></div>
              </div>`;
          } else if (type === 'commercial') {
            fields = `
              <div class="fg2">
                <div class="field"><label>Address</label><input class="fi" type="text" id="commercial_comp_${n}_address" placeholder="456 Chestnut St, Philadelphia PA" value="${pre.address||''}"></div>
                <div class="field"><label>Distance (mi)</label><input class="fi" type="number" step="0.1" id="commercial_comp_${n}_distance_miles" placeholder="0.5" value="${pre.distance_miles||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Use Type</label>
                  <select class="fi" id="commercial_comp_${n}_use_type">
                    <option value="">— Select —</option>
                    <option value="Office" ${pre.use_type==='Office'?'selected':''}>Office</option>
                    <option value="Retail" ${pre.use_type==='Retail'?'selected':''}>Retail</option>
                    <option value="Medical" ${pre.use_type==='Medical'?'selected':''}>Medical</option>
                    <option value="Industrial" ${pre.use_type==='Industrial'?'selected':''}>Industrial</option>
                    <option value="Mixed-Use" ${pre.use_type==='Mixed-Use'?'selected':''}>Mixed-Use</option>
                  </select>
                </div>
                <div class="field"><label>Sq Ft</label><input class="fi" type="number" id="commercial_comp_${n}_sq_ft" placeholder="3500" value="${pre.sq_ft||''}"></div>
                <div class="field"><label>Asking Rent/SF ($)</label><input class="fi" type="number" step="0.01" id="commercial_comp_${n}_asking_rent_per_sf" placeholder="18.50" value="${pre.asking_rent_per_sf||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Lease Type</label>
                  <select class="fi" id="commercial_comp_${n}_lease_type">
                    <option value="">— Select —</option>
                    <option value="NNN" ${pre.lease_type==='NNN'?'selected':''}>NNN</option>
                    <option value="Gross" ${pre.lease_type==='Gross'?'selected':''}>Gross</option>
                    <option value="MG" ${pre.lease_type==='MG'?'selected':''}>Modified Gross</option>
                    <option value="FSG" ${pre.lease_type==='FSG'?'selected':''}>Full Service Gross</option>
                  </select>
                </div>
                <div class="field"><label>Tenant Name</label><input class="fi" type="text" id="commercial_comp_${n}_tenant_name" placeholder="Tenant or Vacant" value="${pre.tenant_name||''}"></div>
                <div class="field"><label>Lease Date</label><input class="fi" type="date" id="commercial_comp_${n}_lease_date" value="${pre.lease_date||''}"></div>
              </div>
              <div class="fg1">
                <div class="field"><label>Source</label><input class="fi" type="text" id="commercial_comp_${n}_source" placeholder="CoStar, LoopNet, Crexi, Manual entry…" value="${pre.source||''}"></div>
              </div>`;
          } else {
            fields = `
              <div class="fg2">
                <div class="field"><label>Address</label><input class="fi" type="text" id="sale_comp_${n}_address" placeholder="789 Market St, Philadelphia PA" value="${pre.address||''}"></div>
                <div class="field"><label>Distance (mi)</label><input class="fi" type="number" step="0.1" id="sale_comp_${n}_distance_miles" placeholder="0.4" value="${pre.distance_miles||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Asset Type</label><input class="fi" type="text" id="sale_comp_${n}_asset_type" placeholder="Office, Retail, Multifamily…" value="${pre.asset_type||''}"></div>
                <div class="field"><label>Sq Ft</label><input class="fi" type="number" id="sale_comp_${n}_sq_ft" placeholder="6000" value="${pre.sq_ft||''}"></div>
                <div class="field"><label>Units</label><input class="fi" type="number" id="sale_comp_${n}_num_units" placeholder="0" value="${pre.num_units||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Sale Price ($)</label><input class="fi" type="number" id="sale_comp_${n}_sale_price" placeholder="1250000" value="${pre.sale_price||''}"></div>
                <div class="field"><label>Price/SF ($)</label><input class="fi" type="number" step="0.01" id="sale_comp_${n}_price_per_sf" placeholder="208.33" value="${pre.price_per_sf||''}"></div>
                <div class="field"><label>Price/Unit ($)</label><input class="fi" type="number" id="sale_comp_${n}_price_per_unit" placeholder="0" value="${pre.price_per_unit||''}"></div>
              </div>
              <div class="fg3">
                <div class="field"><label>Cap Rate (%)</label><input class="fi" type="number" step="0.01" id="sale_comp_${n}_cap_rate" placeholder="6.50" value="${pre.cap_rate||''}"></div>
                <div class="field"><label>Sale Date</label><input class="fi" type="date" id="sale_comp_${n}_sale_date" value="${pre.sale_date||''}"></div>
                <div class="field"><label>Source</label><input class="fi" type="text" id="sale_comp_${n}_source" placeholder="CoStar, MLS, Manual entry…" value="${pre.source||''}"></div>
              </div>`;
          }
          return `
            <div class="comp-entry" style="background:#faf8f4;border:1px solid #ddd;border-radius:6px;padding:14px;margin-bottom:12px;position:relative;">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <div style="font-size:12px;font-weight:600;color:#5C3D26;">${label} #${n}</div>
                <button type="button" onclick="removeComp('${type}',${n})" style="background:none;border:none;color:#8B2020;cursor:pointer;font-size:13px;padding:2px 6px;" title="Remove">✕ Remove</button>
              </div>
              ${fields}
            </div>`;
        }

        function collectComps() {
          const result = { rent_comps: [], commercial_comps: [], sale_comps: [] };
          ['rent','commercial','sale'].forEach(type => {
            const container = document.getElementById(type + '-comps-container');
            if (!container) return;
            const entries = container.querySelectorAll('.comp-entry');
            entries.forEach((entry, i) => {
              const n = i + 1;
              const get = (field) => {
                const el = document.getElementById(type + '_comp_' + n + '_' + field);
                if (!el) return null;
                const v = el.value.trim();
                if (v === '' || v === '0') return null;
                return isNaN(v) ? v : parseFloat(v);
              };
              let comp = {};
              if (type === 'rent') {
                comp = { address: get('address'), distance_miles: get('distance_miles'),
                  unit_type: get('unit_type'), beds: get('beds'), baths: get('baths'),
                  sq_ft: get('sq_ft'), monthly_rent: get('monthly_rent'),
                  rent_per_sf: get('rent_per_sf'), lease_date: get('lease_date'), source: get('source') };
              } else if (type === 'commercial') {
                comp = { address: get('address'), distance_miles: get('distance_miles'),
                  use_type: get('use_type'), sq_ft: get('sq_ft'),
                  asking_rent_per_sf: get('asking_rent_per_sf'), lease_type: get('lease_type'),
                  lease_date: get('lease_date'), tenant_name: get('tenant_name'), source: get('source') };
              } else {
                comp = { address: get('address'), distance_miles: get('distance_miles'),
                  asset_type: get('asset_type'), sq_ft: get('sq_ft'), num_units: get('num_units'),
                  sale_price: get('sale_price'), price_per_sf: get('price_per_sf'),
                  price_per_unit: get('price_per_unit'), cap_rate: get('cap_rate'),
                  sale_date: get('sale_date'), source: get('source') };
              }
              if (Object.values(comp).some(v => v !== null)) {
                const key = type === 'rent' ? 'rent_comps' : type === 'commercial' ? 'commercial_comps' : 'sale_comps';
                result[key].push(comp);
              }
            });
          });
          return result;
        }

        function prefillCompsFromExtraction(compsData) {
          if (!compsData) return;
          (compsData.rent_comps || []).forEach(c => addComp('rent', c));
          (compsData.commercial_comps || []).forEach(c => addComp('commercial', c));
          (compsData.sale_comps || []).forEach(c => addComp('sale', c));
        }
        // ── /Comp Entry Logic ─────────────────────────────────────────────
'''

COMP_CSS = '''
        .btn-add-comp {
          background: #f0ebe0;
          border: 1px dashed #8B6914;
          color: #5C3D26;
          padding: 7px 16px;
          border-radius: 4px;
          cursor: pointer;
          font-size: 12px;
          font-weight: 600;
          margin-top: 4px;
          transition: background 0.15s;
        }
        .btn-add-comp:hover { background: #e8dfc8; }
        .comp-entry .fi { font-size: 12px; }
'''

# 1. Insert CSS — find end of existing style block
css_insert_point = content.rfind('</style>')
content = content[:css_insert_point] + COMP_CSS + '\n      ' + content[css_insert_point:]

# 2. Insert card HTML — after the Sale & Disposition card closing div, before Step 03 Documents
insert_marker = '<!-- Step 03'
if insert_marker not in content:
    insert_marker = 'Document Upload'
idx = content.find(insert_marker)
if idx == -1:
    print("ERROR: Could not find insert marker for card HTML")
else:
    content = content[:idx] + COMP_CARD + '\n        ' + content[idx:]

# 3. Insert JS — before closing </script> tag (last one)
js_insert = content.rfind('</script>')
content = content[:js_insert] + COMP_JS + '\n      ' + content[js_insert:]

# 4. Wire collectComps() into the payload — find where payload is built
payload_search = 'uploaded_files'
idx2 = content.rfind(payload_search)
print('uploaded_files last occurrence at:', idx2)
if idx2 >= 0:
    snippet = content[max(0,idx2-200):idx2+300]
    print(snippet.encode('ascii', 'replace').decode('ascii'))

with open('fp_underwriting_FINAL_v7.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('\nHTML updated — comp card, CSS, and JS injected')
print('File size now:', len(content))
