async function init() {
  try {
    const res = await fetch('../preview.json');
    const data = await res.json();
    if (!data.length) throw new Error('preview.json is empty');
    const first = data[0];

    // Set page title
    document.getElementById('page-title').textContent = `${first.format_1.toUpperCase()} → ${first.format_2.toUpperCase()} converter`;

    // Inject landing HTML (already sanitized as basic HTML)
    document.getElementById('landing-content').innerHTML = first.content;

    // Render comparison table
    if (first.comparison_table) {
      const tbl = document.createElement('table');
      tbl.className = 'comp-table';
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      first.comparison_table.headers.forEach(h => {
        const th = document.createElement('th');
        th.textContent = h;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      tbl.appendChild(thead);
      const tbody = document.createElement('tbody');
      first.comparison_table.rows.forEach(r => {
        const tr = document.createElement('tr');
        r.forEach(cell => {
          const td = document.createElement('td');
          td.textContent = cell;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody);
      document.getElementById('landing-content').appendChild(tbl);
    }

    // Build FAQ accordion
    const faqEl = document.getElementById('faq');
    first.faq.forEach((q, i) => {
      const item = document.createElement('div');
      item.className = 'accordion-item';
      const btn = document.createElement('button');
      btn.className = 'accordion-button';
      btn.textContent = q.question;
      btn.addEventListener('click', () => item.classList.toggle('open'));
      const body = document.createElement('div');
      body.className = 'accordion-content';
      body.innerHTML = `<p>${q.answer}</p>`;
      item.append(btn, body);
      faqEl.appendChild(item);
    });

    // Blog ideas cards
    const blogsEl = document.getElementById('blogs');
    first.blog_ideas.forEach((b, idx) => {
      const link = document.createElement('a');
      link.className = 'card-link';
      link.href = `blog_${idx + 1}.html`;
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `<h3>${b.title}</h3><p>${b.meta}</p>`;
      link.appendChild(card);
      blogsEl.appendChild(link);
    });

    // Use cases cards
    const usesEl = document.getElementById('uses');
    first.use_cases.forEach((u, idx) => {
      const link = document.createElement('a');
      link.className = 'card-link';
      link.href = `use_${idx + 1}.html`;
      const card = document.createElement('div');
      card.className = 'card';
      card.innerHTML = `<h3>${u.name}</h3><p>${u.description}</p>`;
      link.appendChild(card);
      usesEl.appendChild(link);
    });
  } catch (err) {
    console.error(err);
    document.body.innerHTML = '<p style="color:red">Error loading preview.json</p>';
  }
}

init(); 