import json
import os
import re
import subprocess
import jinja2
from datetime import datetime

_METRIC_NEEDED_RE = re.compile(r'\s*\[METRIC NEEDED\]', re.IGNORECASE)
_FS_UNSAFE_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename_component(text: str, fallback: str = "Unknown") -> str:
    """
    Make a string safe to use as a folder/file name component: strips
    filesystem-illegal characters (notably '/', which would otherwise be
    read as a path separator and silently create subdirectories), collapses
    whitespace to underscores. Shared by generate_pdfs() (the PDF filename)
    and engine/graph.py's finalizer_node (the output folder name) so both
    use the identical company+title naming — see either caller's docstring
    for why: multiple applications to the same company for different roles
    were indistinguishable by folder/filename alone before this existed.
    """
    text = (text or "").strip() or fallback
    text = _FS_UNSAFE_RE.sub("", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def sanitize_output_text(obj):
    """
    Single source of truth for output-text hygiene, applied to every PDF this
    pipeline generates (tailored via graph.py, deterministic via
    generate_generic.py, cover-letter-only regen via cl_writer.py — all of
    them funnel through generate_pdfs() below, so fixing it here fixes it
    everywhere at once instead of duplicating the rule per caller).

    Rules:
      - No em dash (—) anywhere, in LLM output OR master data. Replaced with
        a comma. It's a well-known AI-generation tell, so it's banned
        outright regardless of source — this has caught literal em dashes
        sitting in hand-written master data, not just model output.
      - No literal [METRIC NEEDED] internal review tag reaching a PDF (it's
        meant for run_summary.txt only).
      - No mid-sentence pipe character (reserved for the LaTeX header).
    Recurses through dicts/lists so it can be applied to a whole data tree.
    """
    if isinstance(obj, str):
        obj = _METRIC_NEEDED_RE.sub("", obj)
        obj = obj.replace("—", ", ")
        obj = obj.replace(" | ", ", ")
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_output_text(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_output_text(v) for v in obj]
    return obj


def escape_latex_chars(obj):
    """Recursively scans the JSON and escapes all 10 LaTeX special characters."""
    if isinstance(obj, str):
        obj = obj.replace('\\', r'\textbackslash{}')
        for char, replacement in [
            ('&',  r'\&'),
            ('%',  r'\%'),
            ('$',  r'\$'),
            ('#',  r'\#'),
            ('_',  r'\_'),
            ('{',  r'\{'),
            ('}',  r'\}'),
            ('~',  r'\textasciitilde{}'),
            ('^',  r'\textasciicircum{}'),
        ]:
            obj = obj.replace(char, replacement)
        return obj
    elif isinstance(obj, dict):
        return {k: escape_latex_chars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [escape_latex_chars(v) for v in obj]
    return obj

def generate_pdfs(json_path, output_dir, gen_resume=True, gen_cl=True, resume_template='resume_template.tex'):
    # 1. Load the Data
    with open(json_path, 'r') as f:
        raw_data = json.load(f)

    # Text hygiene (em dash / stray review tags) applied before anything else
    # touches this data — see sanitize_output_text() docstring for why this is
    # the single enforcement point for the whole pipeline.
    raw_data = sanitize_output_text(raw_data)

    # Grab company for file naming BEFORE escaping — used as an OS filename,
    # not LaTeX content, so it must never carry LaTeX escape sequences (e.g.
    # an underscore becoming a literal backslash in the filename). The output
    # FOLDER (built by engine/graph.py's finalizer_node) carries company+title
    # so same-company/different-role runs land in distinguishable folders;
    # the PDF filename itself stays company-only, matching the original
    # "Ankush_Resume_{Company}.pdf" convention.
    company_name = sanitize_filename_component(raw_data.get("company_name"), "Company")

    # 2. Sanitize data and add the date for the cover letter
    data = escape_latex_chars(raw_data)
    data["date"] = datetime.today().strftime('%B %d, %Y')

    # 3. Configure Jinja2
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
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

    # Absolute path for M1 Mac LaTeX installations
    pdflatex_path = '/Library/TeX/texbin/pdflatex'
    if not os.path.exists(pdflatex_path):
        pdflatex_path = 'pdflatex'

    # 4. Helper function to generate and compile a PDF
    def compile_tex(template_name, output_filename):
        template = latex_jinja_env.get_template(template_name)
        rendered_tex = template.render(**data)
        
        tex_path = os.path.join(output_dir, f"{output_filename}.tex")
        with open(tex_path, 'w') as f:
            f.write(rendered_tex)
            
        print(f"Compiling {output_filename}...")
        try:
            subprocess.run(
                [pdflatex_path, '-interaction=nonstopmode', '-output-directory', output_dir, tex_path],
                check=True,
                stdout=subprocess.DEVNULL 
            )
            # Clean up auxiliary files
            for ext in ['.aux', '.log', '.out']:
                junk_file = os.path.join(output_dir, f"{output_filename}{ext}")
                if os.path.exists(junk_file):
                    os.remove(junk_file)
        except subprocess.CalledProcessError:
            print(f"\nERROR: PDF compilation failed for {output_filename}.")

    # 5. Execute for both documents
    if gen_resume:
        compile_tex(resume_template, f"Ankush_Resume_{company_name}")
    if gen_cl:
        compile_tex('cover_letter_template.tex', f"Ankush_Cover_Letter_{company_name}")
    
    print(f"--- SUCCESS: PDFs generated in {output_dir} ---")