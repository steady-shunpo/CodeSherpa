from archCode2 import run_architect_loop
from devCode2 import run_developer_agent
from tools import extract_final_plan
# from devCode2 import run_developer_agent


user_issue = """Issue: Allow FilePathField path to accept a callable.
Description

I have a special case where I want to create a model containing the path to some local files on the server/dev machine. Seeing as the place where these files are stored is different on different machines I have the following:
import os
from django.conf import settings
from django.db import models
class LocalFiles(models.Model):
        name = models.CharField(max_length=255)
        file = models.FilePathField(path=os.path.join(settings.LOCAL_FILE_DIR, 'example_dir'))
Now when running manage.py makemigrations it will resolve the path based on the machine it is being run on. Eg: /home/<username>/server_files/example_dir
I had to manually change the migration to include the os.path.join() part to not break this when running the migration on production/other machine.
"""

repo_url = 'https://github.com/django/django'

arch_res = run_architect_loop(user_issue)
# arch_res = {'status': 'success', 'content': "THOUGHT: I now have a complete understanding of the ReleaseController's mixed responsibilities. The controller handles:\n\n1. HTTP request/response handling (controller layer)\n2. HAL/resource creation and link processing (should be in ReleaseModelAssembler)\n3. Request body mapping and backward compatibility (should be in ReleaseRequestMapper)\n4. Repository link processing (should be in ReleaseLinkProcessor)\n\nI need to create a plan to extract these responsibilities into dedicated classes while maintaining the existing functionality. Based on the existing ResourceProcessor pattern and the current implementation, I'll create three new classes:\n\n1. ReleaseModelAssembler - for HAL resource creation and link processing\n2. ReleaseRequestMapper - for request body mapping and backward compatibility\n3. ReleaseLinkProcessor - for repository link processing\n\nFINAL_PLAN: Here's the step-by-step implementation plan:\n\n1. Create ReleaseModelAssembler class:\n   - Path: rest/resource-server/src/main/java/org/eclipse/sw360/rest/resourceserver/release/ReleaseModelAssembler.java\n   - Extract createHalReleaseResource and createHalReleaseResourceWithAllDetails methods\n   - Move all HAL resource creation logic from ReleaseController\n   - Implement RepresentationModelProcessor<EntityModel<Release>> interface\n   - Add self links and component links\n\n2. Create ReleaseRequestMapper class:\n   - Path: rest/resource-server/src/main/java/org/eclipse/sw360/rest/resourceserver/release/ReleaseRequestMapper.java\n   - Extract setBackwardCompatibleFieldsInRelease method\n   - Move request body mapping and backward compatibility logic\n   - Handle attachment processing\n   - Convert request maps to Release objects\n\n3. Create ReleaseLinkProcessor class:\n   - Path: rest/resource-server/src/main/java/org/eclipse/sw360/rest/resourceserver/release/ReleaseLinkProcessor.java\n   - Extract getLinkedReleases method logic\n   - Move repository link processing\n   - Handle transitive link resolution\n   - Return CollectionModel<HalResource<ReleaseLink>>\n\n4. Update ReleaseController:\n   - Remove extracted methods and logic\n   - Inject new processor classes\n   - Update method implementations to use the new classes\n   - Maintain existing controller layer responsibilities\n\n5. Update imports and dependencies:\n   - Add new class imports to ReleaseController\n   - Remove unused imports\n   - Update any references to moved methods\n\nThe refactoring will maintain all existing functionality while separating concerns according to the Single Responsibility Principle. The controller will only handle HTTP request/response mapping, while the new classes will handle their respective responsibilities."}

arch_content = extract_final_plan(arch_res["content"])

if arch_res['status'] != 'success':
    #copilot
    pass


dev_res = run_developer_agent(arch_content, repo_url) 



print(arch_res)
print(dev_res)