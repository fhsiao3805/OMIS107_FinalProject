// All text reaches the page through h()/textContent, so user-entered values can't inject markup.
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== 'list') el[key] = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  el.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return el;
}

const state = { user: null, majors: [], selected: new Map() };
const view = document.getElementById('view');
const SELECTED_KEY = 'studyhub.selectedCourses';

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error ?? 'Something went wrong.');
  return data;
}

let toastTimer;
function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

const courseCode = (c) => `${c.subject} ${c.number}`;
const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const when = (s) => `${dateFmt.format(new Date(s.startsAt))}, ${timeFmt.format(new Date(s.startsAt))} – ${timeFmt.format(new Date(s.endsAt))}`;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/** Runs an action from a button, reporting failure instead of leaving the user guessing. */
async function act(fn, successMessage) {
  try {
    await fn();
    if (successMessage) toast(successMessage);
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

/** Submit handler that shows server validation errors at the top of the form. */
function submitter(fn) {
  return async (event) => {
    event.preventDefault();
    const form = event.target;
    form.querySelector('.error')?.remove();
    const button = form.querySelector('[type=submit]');
    button.disabled = true;
    try {
      await fn(formData(form), form);
    } catch (err) {
      form.prepend(h('p', { className: 'error', role: 'alert' }, err.message));
    } finally {
      button.disabled = false;
    }
  };
}

function modal(title, buildBody) {
  const close = () => { dialog.close(); dialog.remove(); };
  const dialog = h('dialog', { ariaLabel: title }, h('h2', {}, title), buildBody(close));
  dialog.addEventListener('cancel', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

const field = (label, control) => h('label', { className: 'field' }, label, control);
const majorOptions = (selected, placeholder) => [
  h('option', { value: '' }, placeholder),
  state.majors.map((m) => h('option', { value: m.id, selected: m.id === selected }, m.name)),
];
const dialogButtons = (close, label) => h('div', { className: 'dialog-actions' },
  h('button', { type: 'button', className: 'btn secondary', onclick: close }, 'Cancel'),
  h('button', { type: 'submit', className: 'btn' }, label));

/** Local date + time inputs become an absolute instant, so everyone sees the right time. */
function rangeFrom({ date, start, end }) {
  return { startsAt: new Date(`${date}T${start}`).toISOString(), endsAt: new Date(`${date}T${end}`).toISOString() };
}

function timeFields() {
  const tomorrow = new Date(Date.now() + 86_400_000);
  const iso = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, '0')}-${String(tomorrow.getDate()).padStart(2, '0')}`;
  return [
    field('Date', h('input', { type: 'date', name: 'date', required: true, value: iso, min: new Date().toLocaleDateString('en-CA') })),
    h('div', { className: 'row' },
      field('Starts', h('input', { type: 'time', name: 'start', required: true, value: '17:00', step: 300 })),
      field('Ends', h('input', { type: 'time', name: 'end', required: true, value: '18:00', step: 300 }))),
  ];
}

function renderNav() {
  const nav = document.getElementById('nav');
  const u = state.user;
  const here = location.hash || (location.pathname === '/home' ? '/home' : '#/login');
  const link = (href, label) => h('a', { href, ariaCurrent: here === href ? 'page' : null }, label);
  nav.replaceChildren(
    link('/home', 'Find help'),
    ...(u
      ? [
        u.role === 'tutor' ? link('#/tutor', 'Tutor dashboard') : link('#/schedule', 'My schedule'),
        link('#/profile', 'Profile'),
        ...(u.roles.length > 1
          ? [u.role === 'tutor'
            ? h('button', { onclick: () => switchRole('student') }, 'Switch to student view')
            : h('button', { onclick: () => switchRole('tutor') }, 'Switch to tutor view')]
          : []),
        h('button', { onclick: logout }, 'Log out'),
      ]
      : [link('#/login', 'Log in'), link('#/signup', 'Sign up')]),
  );
}

const homeHash = () => (state.user.role === 'tutor' ? '#/tutor' : '#/');

function goHome() {
  if (location.hash === homeHash()) route();
  else location.hash = homeHash();
}

async function switchRole(role) {
  const switched = await act(async () => {
    state.user = (await api('/me/role', { method: 'POST', body: { role } })).user;
  }, `You are now in the ${role} view.`);
  if (switched) goHome();
}

async function logout() {
  await act(() => api('/auth/logout', { method: 'POST' }));
  state.user = null;
  location.assign('/');
}

// ---- Find page ----

function saveSelection() {
  localStorage.setItem(SELECTED_KEY, JSON.stringify([...state.selected.keys()]));
}

async function restoreSelection() {
  let ids = [];
  try { ids = JSON.parse(localStorage.getItem(SELECTED_KEY)) ?? []; } catch { /* treat as no selection */ }
  if (ids.length === 0) return;
  const { courses } = await api(`/courses/by-ids?ids=${ids.join(',')}`);
  state.selected = new Map(courses.map((c) => [c.id, c]));
}

async function findPage() {
  const status = await api('/status');
  const search = h('input', {
    type: 'text',
    id: 'course-input',
    role: 'combobox',
    autocomplete: 'off',
    placeholder: 'Choose or type a course, e.g. OMIS 107',
    'aria-autocomplete': 'list',
    'aria-controls': 'course-options',
    'aria-expanded': 'false',
  });
  const list = h('ul', { id: 'course-options', className: 'combo-list', role: 'listbox', hidden: true, 'aria-label': 'Courses' });
  const caret = h('button', { type: 'button', className: 'combo-caret', tabIndex: -1, 'aria-label': 'Show courses' }, '▾');
  const major = h('select', { ariaLabel: 'Filter by major' }, majorOptions(state.user?.majorId, 'All majors'));
  const onlyOpen = h('input', { type: 'checkbox' });
  const count = h('p', { className: 'muted small', ariaLive: 'polite' });
  const right = h('div', {});
  let options = [];
  let total = 0;
  let active = -1;
  let request = 0;
  let loading = null;

  function setActive(index) {
    active = index;
    [...list.querySelectorAll('[role=option]')].forEach((el, i) => el.classList.toggle('active', i === active));
    const current = list.querySelectorAll('[role=option]')[active];
    if (current) {
      search.setAttribute('aria-activedescendant', current.id);
      current.scrollIntoView({ block: 'nearest' });
    } else {
      search.removeAttribute('aria-activedescendant');
    }
  }

  function openList() {
    list.hidden = false;
    search.setAttribute('aria-expanded', 'true');
  }

  function closeList() {
    list.hidden = true;
    search.setAttribute('aria-expanded', 'false');
    setActive(-1);
  }

  async function fetchPage(reset) {
    const id = ++request;
    const params = new URLSearchParams({ q: search.value, limit: 100, offset: reset ? 0 : options.length });
    if (major.value) params.set('major', major.value);
    if (onlyOpen.checked) params.set('available', '1');
    const data = await api(`/courses?${params}`);
    if (id !== request) return; // a newer search has started
    total = data.total;
    if (reset) {
      options = [];
      list.replaceChildren();
      list.scrollTop = 0;
      active = -1;
    }
    list.append(...data.courses.map((c, i) => courseOption(c, options.length + i)));
    options.push(...data.courses);
    if (options.length === 0) list.replaceChildren(h('li', { className: 'combo-empty' }, 'No courses match.'));
    count.textContent = `${plural(total, 'course')} found`;
  }

  function loadCourses(reset) {
    loading = fetchPage(reset).catch((err) => toast(err.message)).finally(() => { loading = null; });
    return loading;
  }

  function courseOption(c, index) {
    const isSelected = state.selected.has(c.id);
    return h('li', {
      id: `course-option-${index}`,
      role: 'option',
      className: 'combo-option',
      'aria-selected': String(isSelected),
      onclick: () => choose(c),
    },
    h('span', { className: 'combo-tick' }, isSelected ? '✓' : ''),
    h('span', {},
      h('span', { className: 'course-code' }, courseCode(c)),
      h('span', { className: 'course-title' }, c.title),
      (c.openSlots > 0 || c.openGroups > 0) && h('span', { className: 'badges' },
        c.openSlots > 0 && h('span', { className: 'badge' }, plural(c.openSlots, 'tutor slot')),
        c.openGroups > 0 && h('span', { className: 'badge' }, plural(c.openGroups, 'group')))));
  }

  /** Picking a course adds it to the selection; picking a selected course removes it. */
  async function choose(c) {
    closeList();
    search.value = '';
    if (state.selected.has(c.id)) {
      state.selected.delete(c.id);
    } else {
      const { courses } = await api(`/courses/by-ids?ids=${c.id}`);
      state.selected.set(c.id, courses[0]);
    }
    saveSelection();
    await Promise.all([renderRight(), loadCourses(true)]);
  }

  function removeCourse(id) {
    state.selected.delete(id);
    saveSelection();
    renderRight();
    loadCourses(true);
  }

  let tab = 'tutoring';

  async function renderRight() {
    const courses = [...state.selected.values()];
    if (courses.length === 0) {
      right.replaceChildren(h('div', { className: 'empty' }, 'Choose a course above to see tutoring times and study groups. You can add more than one.'));
      return;
    }
    let data;
    try {
      data = await api(`/availability?courses=${courses.map((c) => c.id).join(',')}`);
    } catch (err) {
      right.replaceChildren(h('p', { className: 'error' }, err.message));
      return;
    }
    const canCreate = state.user?.role !== 'tutor';
    const tabButton = (key, label) => h('button', {
      className: 'tab', role: 'tab', ariaSelected: String(tab === key), onclick: () => { tab = key; renderRight(); },
    }, label);

    right.replaceChildren(
      h('div', { className: 'selected-head' },
        h('h2', {}, `Selected courses (${courses.length})`),
        h('button', { className: 'link-btn', onclick: () => {
          state.selected.clear();
          saveSelection();
          renderRight();
          loadCourses(true);
        } }, 'Clear all')),
      h('ul', { className: 'selected' }, courses.map((c) => h('li', { className: 'selected-item' },
        h('details', {},
          h('summary', {}, h('strong', {}, courseCode(c)), ` ${c.title}`),
          h('p', { className: 'about' }, c.description || 'No description is listed in the bulletin.'),
          h('p', { className: 'about small' },
            c.units && `${c.units} units · `,
            h('a', { href: c.sourceUrl, target: '_blank', rel: 'noopener' }, 'View in SCU Bulletin'))),
        h('button', { className: 'remove', 'aria-label': `Remove ${courseCode(c)}`, onclick: () => removeCourse(c.id) }, '×')))),
      h('div', { className: 'tabs', role: 'tablist' },
        tabButton('tutoring', `Tutoring (${data.slots.length})`),
        tabButton('groups', `Study groups (${data.groups.length})`),
        tab === 'groups' && canCreate && h('button', { className: 'btn', onclick: () => createGroup(courses) }, 'Create a study group')),
      h('div', { className: 'listing' }, tab === 'tutoring' ? slotCards(data.slots) : groupCards(data.groups, courses)),
    );
  }

  const needLogin = () => { toast('Log in to continue.'); location.hash = '#/login'; };

  function slotCards(slots) {
    if (slots.length === 0) return h('div', { className: 'empty' }, 'No open tutoring times for these courses yet. Try a study group instead.');
    return slots.map((s) => {
      const courses = s.courseIds.map((id) => state.selected.get(id)).filter(Boolean);
      return h('article', { className: 'session' },
        h('div', {},
          h('h3', {}, when(s)),
          h('p', {}, `${s.tutorName} · ${courses.map(courseCode).join(', ')}`),
          h('p', { className: 'muted' }, s.location),
          s.tutorBio && h('p', { className: 'muted small' }, s.tutorBio)),
        state.user?.role !== 'tutor' && h('div', { className: 'actions' },
          s.mine
            ? h('span', { className: 'badge' }, 'Your time slot')
            : h('button', { className: 'btn', onclick: () => (state.user ? bookSlot(s, courses) : needLogin()) }, 'Book')));
    });
  }

  function groupCards(groups, courses) {
    if (groups.length === 0) return h('div', { className: 'empty' }, 'No study groups yet. Create one and others can join.');
    return groups.map((g) => {
      const course = courses.find((c) => c.id === g.courseId);
      const full = g.memberCount >= g.capacity;
      return h('article', { className: 'session' },
        h('div', {},
          h('h3', {}, g.topic),
          h('p', {}, `${courseCode(course)} · ${when(g)}`),
          h('p', { className: 'muted' }, `${g.location} · Started by ${g.creatorName}`),
          h('span', { className: `badge${full ? ' warn' : ''}` }, `${g.memberCount} of ${g.capacity} joined`)),
        state.user?.role !== 'tutor' && h('div', { className: 'actions' },
          g.joined
            ? h('span', { className: 'badge' }, 'You joined')
            : h('button', {
              className: 'btn',
              disabled: full,
              onclick: async () => {
                if (!state.user) return needLogin();
                if (await act(() => api(`/groups/${g.id}/join`, { method: 'POST' }), 'You joined the study group.')) refresh();
              },
            }, full ? 'Full' : 'Join')));
    });
  }

  const refresh = () => { renderRight(); loadCourses(true); };

  function bookSlot(slot, courses) {
    modal('Book this time', (close) => h('form', { onsubmit: submitter(async ({ courseId, note }) => {
      await api(`/slots/${slot.id}/book`, { method: 'POST', body: { courseId, note } });
      close();
      toast('Booked. It is on your schedule now.');
      refresh();
    }) },
    h('p', {}, `${when(slot)} with ${slot.tutorName}`),
    h('p', { className: 'muted small' }, slot.location),
    field('Course', h('select', { name: 'courseId', required: true }, courses.map((c) => h('option', { value: c.id }, `${courseCode(c)} ${c.title}`)))),
    field('What do you need help with? (optional)', h('textarea', { name: 'note', maxLength: 500 })),
    dialogButtons(close, 'Confirm booking')));
  }

  function createGroup(selected) {
    if (!state.user) return needLogin();
    modal('Create a study group', (close) => h('form', { onsubmit: submitter(async (values) => {
      await api('/groups', { method: 'POST', body: { ...values, ...rangeFrom(values) } });
      close();
      toast('Study group created.');
      refresh();
    }) },
    field('Course', h('select', { name: 'courseId', required: true }, selected.map((c) => h('option', { value: c.id }, `${courseCode(c)} ${c.title}`)))),
    field('Topic', h('input', { type: 'text', name: 'topic', required: true, maxLength: 120, placeholder: 'Midterm 2 review' })),
    timeFields(),
    field('Location', h('input', { type: 'text', name: 'location', required: true, maxLength: 120, placeholder: 'Learning Commons, 2nd floor' })),
    field('Maximum group size', h('input', { type: 'number', name: 'capacity', required: true, min: 2, max: 30, value: 6 })),
    dialogButtons(close, 'Create group')));
  }

  let debounce = null;
  search.addEventListener('input', () => {
    openList();
    clearTimeout(debounce);
    debounce = setTimeout(() => { debounce = null; loadCourses(true); }, 150);
  });
  search.addEventListener('click', openList);
  search.addEventListener('focus', openList);
  search.addEventListener('keydown', async (event) => {
    const size = options.length;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (list.hidden) return openList();
      if (size === 0) return undefined;
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActive(active === -1 && step === -1 ? size - 1 : (active + step + size) % size);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      // Typing then pressing Enter quickly must act on the results for the typed text.
      if (debounce !== null) {
        clearTimeout(debounce);
        debounce = null;
        loadCourses(true);
      }
      if (loading) await loading;
      const pick = options[active === -1 ? 0 : active];
      if (pick) choose(pick);
    } else if (event.key === 'Escape') {
      closeList();
    }
    return undefined;
  });
  // Keep focus in the text box while clicking the list, so the click isn't lost to a blur.
  list.addEventListener('mousedown', (event) => event.preventDefault());
  list.addEventListener('scroll', () => {
    const nearEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 80;
    if (nearEnd && !loading && options.length < total) loadCourses(false);
  });
  caret.addEventListener('mousedown', (event) => event.preventDefault());
  caret.addEventListener('click', () => {
    if (list.hidden) { openList(); search.focus(); } else closeList();
  });
  search.addEventListener('blur', closeList);
  major.addEventListener('change', () => loadCourses(true));
  onlyOpen.addEventListener('change', () => loadCourses(true));

  view.replaceChildren(
    h('div', { className: 'intro' },
      h('h1', {}, 'Find help for your classes'),
      h('p', { className: 'muted' }, 'Choose your courses, then book a tutor or join a study group.')),
    ...(status.courses === 0 || status.crawling ? [h('p', { className: 'notice' },
      status.courses === 0
        ? 'The course list is being read from the SCU Bulletin. This takes about 25 minutes the first time.'
        : 'The course list is being refreshed from the SCU Bulletin.')] : []),
    h('div', { className: 'find' },
      h('section', { className: 'card course-panel', ariaLabel: 'Courses' },
        h('label', { className: 'field', htmlFor: 'course-input' }, 'Course'),
        h('div', { className: 'combo' }, search, caret, list),
        field('Filter by major', major),
        h('label', { className: 'check' }, onlyOpen, 'Only courses with open sessions'),
        count),
      h('section', { ariaLabel: 'Tutoring and study groups' }, right)),
  );
  await Promise.all([loadCourses(true), renderRight()]);
}

// ---- Accounts ----

function loginPage() {
  view.replaceChildren(h('div', { className: 'narrow card' },
    h('h1', {}, 'Log in'),
    h('p', {}),
    h('form', { onsubmit: submitter(async (values) => {
      state.user = (await api('/auth/login', { method: 'POST', body: values })).user;
      location.assign('/home');
    }) },
    field('SCU email', h('input', { type: 'email', name: 'email', required: true, autocomplete: 'username' })),
    field('Password', h('input', { type: 'password', name: 'password', required: true, autocomplete: 'current-password' })),
    h('button', { className: 'btn', type: 'submit' }, 'Log in')),
    h('p', { className: 'small muted' }, h('br'), 'New here? ', h('a', { href: '#/signup' }, 'Create an account'))));
}

function signupPage() {
  const role = (value, title, description, checked) => h('label', {},
    h('input', { type: 'radio', name: 'role', value, checked, required: true }), ' ',
    h('strong', {}, title), description);
  view.replaceChildren(h('div', { className: 'narrow card' },
    h('h1', {}, 'Create your account'),
    h('p', {}),
    h('form', { onsubmit: submitter(async (values) => {
      state.user = (await api('/auth/signup', { method: 'POST', body: values })).user;
      location.assign('/home');
    }) },
    h('div', { className: 'roles' },
      role('student', 'Student', 'Book tutors and run study groups', true),
      role('tutor', 'Tutor', 'Offer courses and time slots', false)),
    h('p', { className: 'small muted' }, 'Already a tutor? Choose Student and enter your tutor email and password to use the same account as a student.'),
    field('Full name', h('input', { type: 'text', name: 'name', required: true, maxLength: 80, autocomplete: 'name' })),
    field('SCU email', h('input', { type: 'email', name: 'email', required: true, placeholder: 'you@scu.edu', autocomplete: 'username' })),
    field('Password (at least 8 characters)', h('input', { type: 'password', name: 'password', required: true, minLength: 8, autocomplete: 'new-password' })),
    field('Major (optional)', h('select', { name: 'majorId' }, majorOptions(null, 'Choose your major'))),
    h('button', { className: 'btn', type: 'submit' }, 'Create account')),
    h('p', { className: 'small muted' }, h('br'), 'Already registered? ', h('a', { href: '#/login' }, 'Log in'))));
}

function profilePage() {
  const u = state.user;
  view.replaceChildren(h('div', { className: 'narrow card' },
    h('h1', {}, 'Your profile'),
    h('p', { className: 'muted small' }, `${u.email} · ${u.roles.length > 1 ? 'Tutor and student' : u.role === 'tutor' ? 'Tutor' : 'Student'}`),
    h('form', { onsubmit: submitter(async (values) => {
      state.user = (await api('/me', { method: 'PATCH', body: values })).user;
      toast('Profile saved.');
    }) },
    field('Full name', h('input', { type: 'text', name: 'name', required: true, maxLength: 80, value: u.name })),
    field('Major', h('select', { name: 'majorId' }, majorOptions(u.majorId, 'No major selected'))),
    u.roles.includes('tutor') && field('About you (shown with your time slots)', h('textarea', { name: 'bio', maxLength: 500, value: u.bio })),
    h('button', { className: 'btn', type: 'submit' }, 'Save')),
    u.roles.length === 1 && u.role === 'tutor' && [
      h('p', {}),
      h('h2', {}, 'Student account'),
      h('p', { className: 'muted small' }, 'Book tutors and join study groups with this same email and password.'),
      h('button', {
        className: 'btn secondary',
        onclick: async () => {
          const added = await act(async () => {
            state.user = (await api('/me/student-account', { method: 'POST' })).user;
          }, 'Student account added. You are now in the student view.');
          if (added) goHome();
        },
      }, 'Add student account'),
    ]));
}

// ---- Student schedule ----

async function schedulePage() {
  const { bookings, groups } = await api('/me/schedule');
  const reload = () => schedulePage();

  const bookingCards = bookings.map((b) => h('article', { className: 'session' },
    h('div', {},
      h('h3', {}, `${b.subject} ${b.number} with ${b.tutorName}`),
      h('p', {}, when(b)),
      h('p', { className: 'muted' }, `${b.location} · ${b.tutorEmail}`),
      b.cancelledAt && h('span', { className: 'badge warn' }, 'Cancelled by tutor')),
    !b.cancelledAt && h('div', { className: 'actions' }, h('button', {
      className: 'btn secondary',
      onclick: async () => { if (await act(() => api(`/slots/${b.slotId}/book`, { method: 'DELETE' }), 'Booking cancelled.')) reload(); },
    }, 'Cancel'))));

  const groupItems = groups.map((g) => h('article', { className: 'session' },
    h('div', {},
      h('h3', {}, `${g.subject} ${g.number}: ${g.topic}`),
      h('p', {}, when(g)),
      h('p', { className: 'muted' }, `${g.location} · ${g.members.length} of ${g.capacity} joined`),
      h('p', { className: 'muted small' }, g.members.map((m) => `${m.name} (${m.email})`).join(', '))),
    h('div', { className: 'actions' }, h('button', {
      className: 'btn secondary',
      onclick: async () => {
        const path = g.isCreator ? `/groups/${g.id}` : `/groups/${g.id}/join`;
        if (await act(() => api(path, { method: 'DELETE' }), g.isCreator ? 'Study group cancelled.' : 'You left the group.')) reload();
      },
    }, g.isCreator ? 'Cancel group' : 'Leave'))));

  view.replaceChildren(
    h('div', { className: 'intro' }, h('h1', {}, 'My schedule')),
    h('div', { className: 'stack' },
      h('section', {}, h('h2', {}, 'Tutoring sessions'), h('div', { className: 'listing' },
        bookingCards.length ? bookingCards : h('div', { className: 'empty' }, 'No tutoring booked. ', h('a', { href: '/home' }, 'Find a tutor')))),
      h('section', {}, h('h2', {}, 'Study groups'), h('div', { className: 'listing' },
        groupItems.length ? groupItems : h('div', { className: 'empty' }, 'You are not in any study groups. ', h('a', { href: '/home' }, 'Find one'))))),
  );
}

// ---- Tutor dashboard ----

async function tutorPage() {
  const [{ courses }, { slots }] = await Promise.all([api('/tutor/courses'), api('/tutor/slots')]);
  let teaching = courses;
  const chips = h('ul', { className: 'chips' });
  const results = h('ul', { className: 'results', hidden: true });
  const search = h('input', { type: 'search', placeholder: 'Add a course: CSCI 60, statistics…', ariaLabel: 'Search for a course to teach' });

  const save = () => api('/tutor/courses', { method: 'PUT', body: { courseIds: teaching.map((c) => c.id) } });

  function renderChips() {
    chips.replaceChildren(...(teaching.length
      ? teaching.map((c) => h('li', { className: 'chip', title: c.title }, courseCode(c), h('button', {
        ariaLabel: `Remove ${courseCode(c)}`,
        onclick: async () => {
          const previous = teaching;
          teaching = teaching.filter((t) => t.id !== c.id);
          if (!await act(save)) teaching = previous;
          renderChips();
        },
      }, '×')))
      : [h('li', { className: 'muted small' }, 'Add the courses you can teach. Students only see your times for these courses.')]));
  }

  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(async () => {
      if (!search.value.trim()) { results.hidden = true; return; }
      const { courses: found } = await api(`/courses?${new URLSearchParams({ q: search.value, limit: 20 })}`);
      const options = found.filter((c) => !teaching.some((t) => t.id === c.id));
      results.hidden = options.length === 0;
      results.replaceChildren(...options.map((c) => h('li', {}, h('button', {
        onclick: async () => {
          const previous = teaching;
          teaching = [...teaching, c];
          if (await act(save, `Added ${courseCode(c)}.`)) { search.value = ''; results.hidden = true; } else teaching = previous;
          renderChips();
        },
      }, `${courseCode(c)} ${c.title}`))));
    }, 200);
  });
  renderChips();

  const slotCards = slots.map((s) => h('article', { className: 'session' },
    h('div', {},
      h('h3', {}, when(s)),
      h('p', { className: 'muted' }, s.location),
      s.studentName
        ? [h('p', {}, `Booked by ${s.studentName} (${s.studentEmail}) for ${s.subject} ${s.number}`), s.note && h('p', { className: 'muted small' }, `"${s.note}"`)]
        : h('span', { className: 'badge' }, 'Open')),
    h('div', { className: 'actions' }, h('button', {
      className: 'btn secondary',
      onclick: async () => {
        if (s.studentName && !window.confirm(`Cancel this session with ${s.studentName}? They will see it as cancelled.`)) return;
        if (await act(() => api(`/tutor/slots/${s.id}`, { method: 'DELETE' }), s.studentName ? 'Session cancelled.' : 'Time slot removed.')) tutorPage();
      },
    }, s.studentName ? 'Cancel session' : 'Remove'))));

  view.replaceChildren(
    h('div', { className: 'intro' }, h('h1', {}, 'Tutor dashboard')),
    h('div', { className: 'dash' },
      h('div', { className: 'stack' },
        h('section', { className: 'card' }, h('h2', {}, 'Courses I teach'), chips, h('p', {}), search, results),
        h('section', { className: 'card' }, h('h2', {}, 'Add a time slot'),
          h('form', { onsubmit: submitter(async (values, form) => {
            await api('/tutor/slots', { method: 'POST', body: { location: values.location, repeatWeeks: Number(values.repeatWeeks), ...rangeFrom(values) } });
            form.reset();
            toast('Time slot added.');
            tutorPage();
          }) },
          timeFields(),
          field('Location', h('input', { type: 'text', name: 'location', required: true, maxLength: 120, placeholder: 'Learning Commons or a Zoom link' })),
          field('Repeat weekly for', h('select', { name: 'repeatWeeks' }, [1, 2, 4, 8, 10].map((n) => h('option', { value: n }, n === 1 ? 'Just this once' : `${n} weeks`)))),
          h('button', { className: 'btn', type: 'submit' }, 'Add time slot')))),
      h('section', {}, h('h2', {}, 'Upcoming time slots'), h('div', { className: 'listing' },
        slotCards.length ? slotCards : h('div', { className: 'empty' }, 'No upcoming time slots. Add one to start getting bookings.')))),
  );
}

// ---- Router ----

async function route() {
  const hash = location.hash || (location.pathname === '/home' ? '#/' : '#/login');
  const u = state.user;
  const routes = {
    '#/': findPage,
    '#/login': loginPage,
    '#/signup': signupPage,
    '#/profile': u ? profilePage : loginPage,
    '#/schedule': u?.role === 'student' ? schedulePage : loginPage,
    '#/tutor': u?.role === 'tutor' ? tutorPage : loginPage,
  };
  renderNav();
  try {
    await (routes[hash] ?? findPage)();
  } catch (err) {
    view.replaceChildren(h('p', { className: 'error', role: 'alert' }, err.message));
  }
  window.scrollTo(0, 0);
}

async function start() {
  try {
    const [me, majors] = await Promise.all([api('/me'), api('/majors'), restoreSelection()]);
    state.user = me.user;
    state.majors = majors.majors;
  } catch (err) {
    view.replaceChildren(h('p', { className: 'error', role: 'alert' }, `Could not reach the server: ${err.message}`));
    return;
  }
  window.addEventListener('hashchange', route);
  route();
}

start();
