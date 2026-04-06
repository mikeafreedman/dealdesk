import streamlit as st
import subprocess
import os
import tempfile

st.set_page_config(page_title='FP AUS — LibreOffice Test', layout='wide')
st.title('LibreOffice Headless Compatibility Test')
st.write('Upload a .docx file below. The system will convert it to PDF on the server.')

result = subprocess.run(['which', 'libreoffice'], capture_output=True, text=True)
if result.returncode == 0:
    st.success(f'LibreOffice found at: {result.stdout.strip()}')
else:
    st.error('LibreOffice NOT found. Check packages.txt is committed to GitHub.')

ver = subprocess.run(['libreoffice', '--version'], capture_output=True, text=True)
st.info(f'Version: {ver.stdout.strip()}')

uploaded = st.file_uploader('Upload a .docx file to test conversion', type=['docx'])

if uploaded is not None:
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, 'input.docx')
        with open(input_path, 'wb') as f:
            f.write(uploaded.read())

        st.write('Running LibreOffice conversion...')
        proc = subprocess.run(
            ['libreoffice', '--headless', '--convert-to', 'pdf', input_path, '--outdir', tmpdir],
            capture_output=True, text=True, timeout=60
        )

        st.write(f'Return code: {proc.returncode}')
        st.write(f'STDOUT: {proc.stdout}')
        if proc.stderr:
            st.warning(f'STDERR: {proc.stderr}')

        pdf_path = os.path.join(tmpdir, 'input.pdf')
        if os.path.exists(pdf_path):
            st.success('PDF conversion SUCCESSFUL!')
            with open(pdf_path, 'rb') as f:
                st.download_button(
                    label='Download Converted PDF',
                    data=f.read(),
                    file_name='converted.pdf',
                    mime='application/pdf'
                )
        else:
            st.error('PDF file was NOT created. Conversion failed.')