from pathlib import Path
import sqlite3, shutil

ROOT = Path(__file__).resolve().parent
from flask import Flask, render_template, request, redirect, url_for, jsonify

SEED = ROOT / 'seed.db'
RUNTIME = ROOT / 'runtime.db'
app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))

COMPONENTS = [
    ('Accordion','An accordion is a list of headers that hide or reveal additional content when selected.'),
    ('Alert','An alert keeps users informed of important and sometimes time-sensitive changes.'),
    ('Banner','Banners identify official websites of government organizations in the United States. They also help visitors understand whether a website is official and secure.'),
    ('Breadcrumb','Breadcrumbs provide secondary navigation to help users understand where they are in a website.'),
    ('Button','A button draws attention to important actions with a large selectable surface.'),
    ('Button group','A button group collects similar or related actions.'),
    ('Card','Cards contain content and actions about a single subject.'),
    ('Character count','Character count helps users know how much text they can enter when there is a limit on the number of characters.'),
    ('Checkbox','Checkboxes allow users to select one or more options from a list.'),
    ('Collection','A collection displays a compact list of multiple related items like articles or events. The list links each item to its original source.'),
    ('Combo box','A combo box helps users select an item from a large list of options.'),
    ('Data visualizations','Data visualizations help communicate patterns and relationships in a data set.'),
    ('Date picker','A date picker helps users select a single date.'),
    ('Date range picker','A date range picker helps users select a range between two dates.'),
    ('File input','File input allows users to attach one or multiple files.'),
    ('Footer','A footer serves site visitors who arrive at the bottom of a page without finding what they want.'),
    ('Form','A form allows users to enter information into a page.'),
    ('Header','A header helps users identify a site and provides a quick, organized way to reach the main sections.'),
    ('Icon','Icons help communicate meaning, actions, status, or feedback.'),
    ('Icon list','An icon list reinforces the meaning and visibility of individual list items with a leading icon.'),
    ('Identifier','The identifier communicates a site’s parent agency and displays agency links required by federal laws and policies.'),
    ('In-page navigation','The in-page navigation allows navigation to specific sections on a lengthy content page.'),
    ('Input mask','An input mask is a string expression that constrains input to support valid values.'),
    ('Input prefix/suffix','Use input prefixes and suffixes to show symbols or abbreviations that help users enter the right type of information in a form’s text input.'),
    ('Language selector','The consistent placement, interface, and behavior of the language selection component allows users to find content in the language they prefer.'),
    ('Link','A link connects users to a different page or further information.'),
    ('List','A list organizes information into discrete sequential sections.'),
    ('Memorable date','A select for month followed by two text fields is the easiest way to enter most dates.'),
    ('Modal','A modal is a dialog that focuses the user’s attention on a single task or piece of information.'),
    ('Pagination','Pagination helps users navigate between pages of related content.'),
    ('Process list','A process list displays the steps in a process.'),
    ('Prose','Prose styles provide a readable typographic system for long-form content.'),
    ('Radio buttons','Radio buttons allow users to select one option from a list.'),
    ('Range slider','A range slider lets users choose a value or range along a continuum.'),
    ('Search','Search helps users find content on a site.'),
    ('Select','A select input allows users to choose one option from a list.'),
    ('Side navigation','Side navigation provides secondary navigation for a section of a site.'),
    ('Site alert','A site alert communicates an important message across a site.'),
    ('Step indicator','A step indicator shows progress through a multi-step process.'),
    ('Summary box','A summary box highlights key information for users.'),
    ('Table','A table organizes and presents data in rows and columns.'),
    ('Tag','A tag labels content with a short keyword or phrase.'),
    ('Text input','A text input allows users to enter and edit a single line of text.'),
    ('Time picker','A time picker helps users select a time.'),
    ('Tooltip','A tooltip provides helpful information when a user hovers over or focuses an element.'),
    ('Typography','Typography establishes a clear, consistent hierarchy for content.'),
    ('Validation','Validation communicates whether user input meets requirements.'),
]

def ensure_databases():
    rebuild = False
    if SEED.exists():
        try:
            check = sqlite3.connect(SEED)
            rebuild = check.execute('SELECT COUNT(*) FROM components').fetchone()[0] != len(COMPONENTS)
            check.close()
        except sqlite3.Error:
            rebuild = True
    if rebuild:
        if RUNTIME.exists(): RUNTIME.unlink()
        SEED.unlink()
    if not SEED.exists():
        conn = sqlite3.connect(SEED)
        conn.execute('CREATE TABLE components (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, slug TEXT NOT NULL UNIQUE)')
        for idx, (name, desc) in enumerate(COMPONENTS, 1):
            slug = name.lower().replace(' ','-').replace('/','-')
            conn.execute('INSERT INTO components VALUES (?,?,?,?)', (idx, name, desc, slug))
        conn.commit(); conn.close()
    if not RUNTIME.exists():
        shutil.copyfile(SEED, RUNTIME)

def db_rows():
    ensure_databases()
    conn = sqlite3.connect(RUNTIME); conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT * FROM components ORDER BY id')]
    conn.close(); return rows

@app.context_processor
def shared():
    return {'component_names': [name for name, _ in COMPONENTS]}

@app.route('/')
@app.route('/components/')
@app.route('/components/overview/')
def overview():
    return render_template('overview.html', components=db_rows(), query=request.args.get('q',''))

@app.route('/components/accordion/')
def accordion():
    return render_template('accordion.html')

@app.route('/search/')
def search():
    q = request.args.get('q','').strip()
    return render_template('search.html', query=q)

@app.route('/_reset', methods=['GET','POST'])
@app.route('/api/reset', methods=['GET','POST'])
def reset():
    ensure_databases(); shutil.copyfile(SEED, RUNTIME)
    return jsonify({'ok': True, 'seed': str(SEED.name)})

@app.route('/_health')
def health():
    ensure_databases()
    return jsonify({'ok': True, 'site': 'uswds-docs-demo', 'site_id': 'uswds-docs-demo'})

@app.route('/favicon.ico')
def favicon():
    return ('', 204)

ensure_databases()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
