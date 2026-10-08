"""Deleted sources stay out (slice-1 spec section 14, test_deletion_deny_first.py), for what S1-13
builds: once a material is deleted, nothing of it is read back (its record, versions, passages and
page images), its passages leave its project's index after they were added, and a file shared with
another project stays there. Deletion while a material is read or looked up is in test_materials.py
and test_identifier_lookup.py; memory and the index's own checks come with S1-17 and S1-21."""

import pytest

import synthetic_materials as synthetic
from scholia_app import started
from test_materials import added, project_of, rows, settled

pytestmark = pytest.mark.asyncio


async def test_a_deleted_material_is_read_back_nowhere_and_leaves_its_projects_index(tmp_path):
    async with started(tmp_path / "data") as client:
        mine, theirs = await project_of(client, "Mine"), await project_of(client, "Theirs")
        file = ("paper.pdf", synthetic.paper_pdf())  # one file: each PDF made is a new document
        [paper] = (await added(client, mine, file))["materials"]
        await added(client, theirs, file)
        [ready] = await settled(client, mine)
        [kept] = await settled(client, theirs)
        version = ready["version"]["id"]
        passages = (await client.get(f"/api/material-versions/{version}/passages")).json()["passages"]
        assert (await client.delete(f"/api/materials/{paper['id']}")).status_code == 200
        for path in (f"/api/materials/{paper['id']}", f"/api/materials/{paper['id']}/versions",
                     f"/api/material-versions/{version}/passages", f"/api/material-versions/{version}/pages/1"):
            assert (await client.get(path)).status_code == 404, path
        listed = await client.get(f"/api/projects/{mine}/materials")
        assert listed.json()["materials"] == [], listed.text
        removed = await rows(client, "SELECT project_id, count(*) FROM index_queue WHERE op = 'remove' GROUP BY 1")
        assert removed == [(mine, len(passages))]  # only its own project's rows; the other keeps the shared file
        # The shared extraction is the other project's still: readable there.
        assert (await client.get(f"/api/material-versions/{kept['version']['id']}/passages")).json()["total"] == len(passages)
        one = (await client.get(f"/api/passages/{passages[0]['id']}")).json()
        assert [m["project_id"] for m in one["materials"]] == [theirs]
