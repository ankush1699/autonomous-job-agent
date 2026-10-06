"""
Layer 1 template test — no API calls required.
Renders the resume template against the most recent temp_data.json
and compiles it to PDF so you can inspect template changes instantly.
"""
import json
import jinja2
import os
import subprocess

# Auto-find the most recent output folder in ~/Documents/Resumes/
resumes_dir = os.path.expanduser("~/Documents/Resumes")
folders = [
    f for f in os.listdir(resumes_dir)
    if os.path.isdir(os.path.join(resumes_dir, f))
    and os.path.exists(os.path.join(resumes_dir, f, "temp_data.json"))
]
if not folders:
    raise FileNotFoundError("No temp_data.json found. Run the full pipeline first.")

latest_folder = sorted(folders)[-1]
temp_data_path = os.path.join(resumes_dir, latest_folder, "temp_data.json")
print(f"Using: {temp_data_path}")

with open(temp_data_path, "r") as f:
    data = json.load(f)

# Inject new fields added in the latest pipeline version (safe defaults for old JSON files)
data.setdefault("job_title", "Software Engineer")
data.setdefault("company_name", "Test Company")

# Jinja2 environment — mirrors pdf_generator.py exactly
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
latex_jinja_env = jinja2.Environment(
    block_start_string=r'\BLOCK{',
    block_end_string=r'}',
    variable_start_string=r'\VAR{',
    variable_end_string=r'}',
    comment_start_string=r'\#{',
    comment_end_string=r'}',
    line_statement_prefix='%%',
    line_comment_prefix='%#',
    trim_blocks=True,
    autoescape=False,
    loader=jinja2.FileSystemLoader(os.path.join(root_dir, 'templates'))
)

template = latex_jinja_env.get_template('resume_template.tex')
rendered_tex = template.render(**data)

output_tex = os.path.join(root_dir, 'test_resume.tex')
with open(output_tex, 'w') as f:
    f.write(rendered_tex)

print("Compiled test_resume.tex — running pdflatex...")

pdflatex_path = '/Library/TeX/texbin/pdflatex'
if not os.path.exists(pdflatex_path):
    pdflatex_path = 'pdflatex'

result = subprocess.run(
    [pdflatex_path, '-interaction=nonstopmode', '-output-directory', root_dir, output_tex],
    capture_output=True
)

if result.returncode == 0:
    print("SUCCESS — open test_resume.pdf to inspect.")
else:
    print("FAILED — check test_resume.log for LaTeX errors.")
    print(result.stdout.decode('utf-8', errors='replace')[-2000:])
