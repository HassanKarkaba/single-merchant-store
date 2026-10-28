"""Optional built-image checks; run with SME_CONTAINER_IMAGE after a local build."""
import json
import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not os.environ.get('SME_CONTAINER_IMAGE'),reason='requires a locally built Docker image')
def test_built_image_runs_without_capabilities_and_has_an_empty_state():
    docker=shutil.which('docker')
    assert docker,'Docker CLI is required for the selected image test'
    script="""import json,os,subprocess
checks={
'uid':os.getuid(),
'caddy_capability':'security.capability' in os.listxattr('/usr/local/bin/caddy'),
'state_entries':os.listdir('/state'),
'caddy_version':subprocess.check_output(['/usr/local/bin/caddy','version'],text=True).split()[0],
'python_version':subprocess.check_output(['python','--version'],text=True).strip(),
'postgres_version':subprocess.check_output(['postgres','--version'],text=True).strip(),
'node_version':subprocess.check_output(['node','--version'],text=True).strip(),
}
print(json.dumps(checks))
"""
    result=subprocess.run([docker,'run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges',
                           '--entrypoint','python',os.environ['SME_CONTAINER_IMAGE'],'-c',script],
                          capture_output=True,text=True,timeout=45,check=True)
    checks=json.loads(result.stdout)
    assert checks['uid']==10001
    assert checks['caddy_capability'] is False
    assert checks['state_entries']==[]
    assert checks['caddy_version']=='v2.11.7'
    assert checks['python_version']=='Python 3.12.15'
    assert '(PostgreSQL) 16.15' in checks['postgres_version']
    assert checks['node_version']=='v24.22.1'
