from pathlib import Path
from OCP.Bnd import Bnd_Box
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.STEPControl import STEPControl_ManifoldSolidBrep, STEPControl_Reader, STEPControl_Writer
from OCP.TopAbs import TopAbs_IN, TopAbs_OUT, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec

# Z=0 is the bottom of the drive, X=0 is its width center, and Y=0 is the bezel face.
thickness = 3.8
tray_drive_floor = 5.2
slot_lower_lift = 2.0
media_z = 16.4
button_x, button_z = 32.0, 4.1
led_x, led_z = -28.0, 5.2
led_width, led_height = 6.7, 3.0
led_back_width, led_back_height = 8.7, 5.0
led_step = 3.0
clip_centers = (3.1, 16.6)
receiver_near, receiver_far = 6.5, 8.5
receiver_height = 4.2
assert abs(clip_centers[1] - clip_centers[0] + receiver_height - 17.7) < 1e-8
receiver_inner, receiver_outer = 50.2, 50.8
clip_body_length = 9.1 + 0.5
clip_length = clip_body_length + 1.2

def box(x0, y0, z0, x1, y1, z1):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0,y0,z0), x1-x0, y1-y0, z1-z0).Shape()

def combine(kind, a, b):
    operation = kind(a,b)
    assert operation.IsDone()
    return operation.Shape()

def wire(points):
    polygon = BRepBuilderAPI_MakePolygon()
    for p in points:
        polygon.Add(gp_Pnt(*p))
    polygon.Close()
    return polygon.Wire()

def prism(points, vector):
    face = BRepBuilderAPI_MakeFace(wire(points)).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(*vector)).Shape()

def rounded_rect(x, z, w, h, r, y0=-.1, y1=4.1):
    result = combine(BRepAlgoAPI_Fuse,
                     box(x-w/2+r,y0,z-h/2,x+w/2-r,y1,z+h/2),
                     box(x-w/2,y0,z-h/2+r,x+w/2,y1,z+h/2-r))
    for cx in (x-w/2+r,x+w/2-r):
        for cz in (z-h/2+r,z+h/2-r):
            axis = gp_Ax2(gp_Pnt(cx,y0,cz),gp_Dir(0,1,0))
            result = combine(BRepAlgoAPI_Fuse,result,BRepPrimAPI_MakeCylinder(axis,r,y1-y0).Shape())
    return result

def mouth_outline(y, width, height):
    a, b, corner = width/2, height/2, 0.9
    lower = -b + slot_lower_lift
    profile = [(-a+corner,lower), (a-corner,lower), (a,lower+corner), (a,b-corner),
               (a-corner,b), (-a+corner,b), (-a,b-corner), (-a,lower+corner)]
    return wire((x,y,z+media_z) for x,z in profile)

def clip(side, center_z):
    # The root is wider than the tip. The back taper is the lead-in,
    # while the nearly square shoulder catches the sheet-metal opening.
    y0 = thickness - .4
    y1 = thickness + clip_length
    taper_end = thickness + clip_body_length
    outline = [(47.9,y0), (49.7,y0), (49.7,thickness+1.1),
               (49.7,thickness+6.65), (50.65,thickness+6.8), (50.65,thickness+7.3),
               (49.7,taper_end), (49.7,y1), (48.55,y1), (48.55,thickness+1.6)]
    beam = prism(((side*x,y,center_z-1.95) for x,y in outline),(0,0,3.9))
    taper = prism(((-53,y0,center_z-1.95),(-53,taper_end,center_z-1.4),
                   (-53,y1,center_z-1.4),(-53,y1,center_z+1.4),
                   (-53,taper_end,center_z+1.4),(-53,y0,center_z+1.95)),(106,0,0))
    return combine(BRepAlgoAPI_Common, beam, taper)

folder = Path(__file__).resolve().parent
tray_reader = STEPControl_Reader()
assert tray_reader.ReadFile(str(folder / 'JU-256A046PC-U-tray.step')) == IFSelect_RetDone
assert tray_reader.TransferRoots() == 1
tray = tray_reader.OneShape()
tray_bounds = Bnd_Box()
BRepBndLib.AddOptimal_s(tray, tray_bounds)
tray_min = tray_bounds.CornerMin()
tray_max = tray_bounds.CornerMax()
xmin, xmax = tray_min.X(), tray_max.X()
zmin, zmax = tray_min.Z() - tray_drive_floor, tray_max.Z() - tray_drive_floor
width, height = xmax - xmin, zmax - zmin

panel = box(xmin,0,zmin,xmax,thickness,zmax)
for side in (-1,1):
    for center_z in clip_centers:
        panel = combine(BRepAlgoAPI_Fuse, panel, clip(side,center_z))

slot = BRepOffsetAPI_ThruSections(True, True, 1e-7)
slot.AddWire(mouth_outline(-.1, 96.3, 11.0))
slot.AddWire(mouth_outline(1.35, 94.5, 9.8))
slot.AddWire(mouth_outline(thickness+.1, 94.5, 9.8))
slot.Build()
assert slot.IsDone()
openings = [
    slot.Shape(),
    rounded_rect(0,21.6,27.0,4.5,.9),
    rounded_rect(button_x,button_z,12.7,5.7,.75),
    rounded_rect(button_x,5.2,20.4,10.8,1.0,y0=1.55),
    rounded_rect(led_x,led_z,led_width,led_height,.65),
    rounded_rect(led_x,led_z,led_back_width,led_back_height,.75,y0=led_step),
]
for opening in openings:
    panel = combine(BRepAlgoAPI_Cut, panel, opening)

merge = ShapeUpgrade_UnifySameDomain(panel, True, True, False)
merge.Build()
panel = merge.Shape()
assert BRepCheck_Analyzer(panel).IsValid()

path = folder / 'JU-256A046PC-front-bezel-clip-r8.step'
writer = STEPControl_Writer()
assert writer.Transfer(panel, STEPControl_ManifoldSolidBrep) == IFSelect_RetDone
assert writer.Write(str(path)) == IFSelect_RetDone
reader = STEPControl_Reader()
assert reader.ReadFile(str(path)) == IFSelect_RetDone
assert reader.TransferRoots() == 1
part = reader.OneShape()
assert BRepCheck_Analyzer(part).IsValid()
solids = TopExp_Explorer(part, TopAbs_SOLID)
assert solids.More()
solid = solids.Current()
solids.Next()
assert not solids.More()

clearances = [
    box(-45.2,-.1,media_z-1.8,45.2,thickness+.1,media_z+1.8),
    box(button_x-5.85,-.1,button_z-2.35,button_x+5.85,thickness+.1,button_z+2.35),
    box(button_x-9.0,1.56,.8,button_x+9.0,thickness+.1,10.0),
]
for clearance in clearances:
    intersection = combine(BRepAlgoAPI_Common,part,clearance)
    volume = GProp_GProps()
    BRepGProp.VolumeProperties_s(intersection,volume)
    assert abs(volume.Mass()) < 1e-6

classifier = BRepClass3d_SolidClassifier(solid)
for side in (-1,1):
    for z in clip_centers:
        for x,y in ((49.1,thickness+.3),(50.4,thickness+7.15)):
            classifier.Perform(gp_Pnt(side*x,y,z), 1e-7)
            assert classifier.State() == TopAbs_IN
# Fit against both copies of the receiver envelope estimated from the photo.
for side in (-1,1):
    x0, x1 = sorted((side*receiver_inner, side*receiver_outer))
    receiver = box(x0,thickness,0,x1,thickness+12,height)
    for z in clip_centers:
        receiver = combine(BRepAlgoAPI_Cut,receiver,
                           box(x0-.1,thickness+receiver_near,z-receiver_height/2,
                               x1+.1,thickness+receiver_far,z+receiver_height/2))
    interference = combine(BRepAlgoAPI_Common,part,receiver)
    volume = GProp_GProps()
    BRepGProp.VolumeProperties_s(interference,volume)
    assert abs(volume.Mass()) < 1e-6
# Verify both sides of the lowered disk lip and all four extended tips.
for x in (-28.0,28.0):
    for z,state in ((13.49,TopAbs_IN),(13.51,TopAbs_OUT)):
        classifier.Perform(gp_Pnt(x,2.0,z),1e-7)
        assert classifier.State() == state
for side in (-1,1):
    for z in clip_centers:
        classifier.Perform(gp_Pnt(side*49.1,thickness+clip_length-.05,z),1e-7)
        assert classifier.State() == TopAbs_IN

# Check opening widths, heights and step depth after STEP export and reimport.
def assert_led_profile(y, w, h):
    for dx, dz in ((1,0),(-1,0),(0,1),(0,-1)):
        for clearance in (-.01,.01):
            x = led_x + dx * (w/2 + clearance)
            z = led_z + dz * (h/2 + clearance)
            classifier.Perform(gp_Pnt(x,y,z), 1e-7)
            assert (classifier.State() == TopAbs_IN) == (clearance > 0), (x,y,z)

for y in (.2, led_step - .01):
    assert_led_profile(y,led_width,led_height)
for y in (led_step + .01, thickness - .1):
    assert_led_profile(y,led_back_width,led_back_height)
print('LED: 6.7 x 3.0 mm through opening, 8.7 x 5.0 mm rear pocket begins at 3.0 mm')
bounds = Bnd_Box()
BRepBndLib.AddOptimal_s(part,bounds)
size = [round(b-a,2) for a,b in zip(bounds.CornerMin().Coord(),bounds.CornerMax().Coord())]
assert size == [109.6, 14.6, 30.6]
for x in (-13.0,0.0,13.0):
    for y in (0.2,thickness-0.2):
        classifier.Perform(gp_Pnt(x,y,8.0),1e-7)
        assert classifier.State() == TopAbs_IN
print(f'{path.name}: {size} mm; valid single solid; disk path, button and four hooks checked')




# Map (x, y, z) to (x, -z, y). The exterior face at y=0 lies on the build plate.
from math import pi
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer
from OCP.gp import gp_Ax1, gp_Trsf
rotation = gp_Trsf()
rotation.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1,0,0)),pi/2)
oriented = BRepBuilderAPI_Transform(part,rotation,True).Shape()
bounds = Bnd_Box()
BRepBndLib.AddOptimal_s(oriented,bounds)
assert abs(bounds.CornerMin().Z()) < 1e-5, bounds.CornerMin().Z()
assert abs(bounds.CornerMax().Z()-14.6) < 1e-5, bounds.CornerMax().Z()
BRepMesh_IncrementalMesh(oriented,.025,False,.12,True)
assert StlAPI_Writer().Write(oriented,str(path.with_name('JU-256A046PC-front-bezel-r8-face-down.stl')))


# Align the tray's front to the bezel rear, using the drive support height as Z datum.
fit_transform = gp_Trsf()
fit_transform.SetTranslation(gp_Vec(0, thickness-tray_min.Y(), -tray_drive_floor))
assembled_tray = BRepBuilderAPI_Transform(tray, fit_transform, True).Shape()
contact = combine(BRepAlgoAPI_Common, part, assembled_tray)
fit_volume = GProp_GProps()
BRepGProp.VolumeProperties_s(contact, fit_volume)
assert abs(fit_volume.Mass()) < 1e-6
print(f'Tray match: width {width:.1f} mm, height {height:.1f} mm, no solid interference')
